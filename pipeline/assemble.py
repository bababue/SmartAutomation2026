"""Turn per-page extractions into canonical `Borehole` records.

- Sheets of the same hole within a document are merged (continuation layers joined).
- Coordinates from summary tables are linked to logs by hole name.
- A coordinate system stated elsewhere in the document (other holes, tables, notes, report text) is used
  when a log does not name one; positions far from the other holes of a document are flagged.
- Units are normalised here, in code, never by the model.
- The same hole appearing in several documents (e.g. PDF and PNG export) is de-duplicated.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from schema.borehole import (
    SPT,
    Borehole,
    CoreRun,
    GeorefMethod,
    GroundwaterObservation,
    InvestigationType,
    LabParameter,
    LabResult,
    Layer,
    Sample,
    SecondaryComponent,
    Lithology,
    SourceRef,
)

from .extraction_models import PageExtraction, PageLabResults, XCoordinateRow, XLog
from .georef import crs_from_hint, georeference, haversine_km
from .scoring import score

LOG_KINDS = {"borehole_log", "test_pit_log", "probe_log"}


@dataclass
class PageResult:
    doc_id: str
    source_file: str
    page: int
    kind: str
    extraction: PageExtraction | None
    text: str = ""
    crs_text: str | None = None  # coordinate system statement from the page classification
    labs: PageLabResults | None = None  # rows of a lab result sheet


def _name_key(name: str, separate_numbers: bool) -> str:
    s = name.upper().replace("|", " ")
    if separate_numbers:  # 'BH1-01' and 'BH10-1' stay apart
        s = re.sub(r"(?<=\d)[^A-Z0-9]+(?=\d)", "|", s)
    s = re.sub(r"[^A-Z0-9|]", "", s)
    return re.sub(r"NO(?=\d)", "", s).replace("|", "-")  # 'BH No. 1' → 'BH1'


def id_key(name: str, separate_numbers: bool = False) -> str:
    """Name part of the borehole ID: 'BH No. 01' → 'BH01'. Keeps leading zeros so IDs stay stable.
    `separate_numbers` keeps a '-' between number groups, for names that would otherwise collide."""
    return _name_key(name, separate_numbers) or name.upper()


def norm_name(name: str) -> str:
    """Key for matching sheets, tables and documents: ignores separators and leading zeros
    ('BH-01' = 'BH 1' = 'BH No. 1'), but keeps number groups apart ('BH1-01' = 'BH1-1' ≠ 'BH10-1')."""
    return re.sub(r"(?<!\d)0+(?=\d)", "", _name_key(name, separate_numbers=True)) or name.upper()


def _unique_ids(records: list[Borehole]) -> None:
    """Holes whose plain IDs collide ('BH1-01' and 'BH10-1' → 'BH101') keep their number groups apart."""
    seen = Counter(bh.borehole_id for bh in records)
    for bh in records:
        if seen[bh.borehole_id] > 1:
            doc_id = bh.borehole_id.split(":", 1)[0]
            bh.borehole_id = f"{doc_id}:{id_key(bh.borehole_name, separate_numbers=True)}"


# Type words in hole names. Reports number boreholes and test pits separately ('1'..'6' and '1'..'20'),
# so the type is part of the matching key and the word itself is not.
_PIT_WORDS = re.compile(r"^\s*(?:test\s*pit|trial\s*pit|t\.?\s*p\.?|pit|calicata|cata|schurf)(?![a-z])\.?\s*"
                        r"(?:no\.?|nr\.?|n[º°]\.?)?\s*", re.I)
# Borehole words in the languages of the data ('Hull nr. 7' is Norwegian for hole no. 7).
_HOLE_WORDS = re.compile(r"^\s*(?:bore\s*hole|boring|bh|sondeo|drill\s*hole|borhull|borehull|hull|bohrung|bohrloch|"
                         r"kernbohrung|sondagem|furo|forage|sondage|sondaj)(?![a-z])\.?\s*(?:no\.?|nr\.?|n[º°]\.?)?\s*",
                         re.I)
# Probings are other investigations than boreholes with the same number ('CPT 1' is not 'BH 1').
_PROBE_WORDS = re.compile(r"^\s*(?:cptu?|dpsh|dpl|dph|dp|totalsondering|trykksondering|dreiesondering|sondering|"
                          r"rammsondierung|sondierung|sonda)(?![a-z])\.?\s*(?:no\.?|nr\.?)?\s*", re.I)
_KINDS = {"test_pit_log": "test_pit", "probe_log": "probe", "borehole_log": "borehole"}


def hole_key(name: str, kind: str | None) -> tuple[str | None, str]:
    """(type, number) for matching logs, tables and lab rows: 'Test Pit No. 2' and 'T.P. 2' are the test pit
    '2', 'BH-2' is the borehole '2'. `kind` (from the page or the model) applies when the name has no type word."""
    for words, word_kind in ((_PIT_WORDS, "test_pit"), (_PROBE_WORDS, "probe"), (_HOLE_WORDS, "borehole")):
        rest = words.sub("", name, count=1)
        if rest != name and re.search(r"\d", rest):
            return word_kind, norm_name(rest)
    return (kind if kind in ("borehole", "test_pit", "probe") else None), norm_name(name)


def lookup(index: dict[tuple[str | None, str], object], key: tuple[str | None, str]):
    """Exact (type, number) match; without a known type only a number that is unique across types."""
    if key in index:
        return index[key]
    if key[0] is None:
        hits = [v for (kind, n), v in index.items() if n == key[1]]
        return hits[0] if len(hits) == 1 else None
    return index.get((None, key[1]))


def _unique_ids_by_type(records: list[Borehole]) -> None:
    """Borehole '2' and test pit '2' of one report: the test pit's id gets a 'TP' prefix."""
    seen = Counter(bh.borehole_id for bh in records)
    for bh in records:
        if seen[bh.borehole_id] > 1 and bh.investigation_type and bh.investigation_type.value == "test_pit":
            doc_id, rest = bh.borehole_id.split(":", 1)
            bh.borehole_id = f"{doc_id}:TP{rest}"


def _num(raw: str | None) -> float | None:
    """First numeric literal incl. exponent ('1.2E-6', '3,5x10-7'); decimal comma accepted."""
    if not raw:
        return None
    text = raw.replace(" ", "").replace("×", "x")
    m = re.search(r"(-?\d+(?:[.,]\d+)?)(?:[eE]([-+]?\d+)|x10\^?([-+−]?\d+))?", text)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    exponent = m.group(2) or (m.group(3) or "").replace("−", "-")
    return value * 10 ** int(exponent) if exponent else value


def _date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


# Unit conversion for lab values: (parameter, unit pattern) → factor to the target unit.
# Units each parameter is stored in, as printed without conversion (see LAB_UNITS); a value with any
# other unit that has no factor below is dropped rather than published in the wrong unit.
_STRENGTH = {LabParameter.ucs, LabParameter.tensile_strength, LabParameter.point_load_index, LabParameter.e_modulus}
_LAB_NATIVE: list[tuple[set[LabParameter], str]] = [
    (_STRENGTH, r"^(mpa|n/mm2)$"),
    ({LabParameter.cohesion, LabParameter.undrained_shear_strength}, r"^(kpa|kn/m2)$"),
    ({LabParameter.unit_weight, LabParameter.dry_unit_weight}, r"^kn/m3$"),
    ({LabParameter.water_content, LabParameter.consistency, LabParameter.fines, LabParameter.sand, LabParameter.gravel,
      LabParameter.liquid_limit, LabParameter.plastic_limit}, r"^(%|percent)$"),
    ({LabParameter.friction_angle}, r"^(°|deg|degrees?)$"),
    ({LabParameter.hydraulic_conductivity}, r"^m/s(ec)?$"),
]
_DIMENSIONLESS = {LabParameter.cai, LabParameter.rmr, LabParameter.q, LabParameter.gsi, LabParameter.plasticity_index,
                  LabParameter.consistency_index, LabParameter.poissons_ratio, LabParameter.other}

_LAB_FACTORS: list[tuple[set[LabParameter], str, float]] = [
    (_STRENGTH, r"^psi$", 0.00689476),
    (_STRENGTH, r"^ksi$", 6.89476),
    (_STRENGTH, r"^kn/m2$", 0.001),
    ({LabParameter.cohesion, LabParameter.undrained_shear_strength}, r"^psi$", 6.89476),
    ({LabParameter.cohesion, LabParameter.undrained_shear_strength}, r"^t/?m2$", 9.80665),
    ({LabParameter.unit_weight, LabParameter.dry_unit_weight}, r"^pcf$|^lb/ft3$", 0.157087),
    ({LabParameter.ucs, LabParameter.tensile_strength, LabParameter.point_load_index, LabParameter.e_modulus}, r"^kpa$", 0.001),
    ({LabParameter.ucs, LabParameter.tensile_strength, LabParameter.point_load_index, LabParameter.e_modulus}, r"^kg/?cm2|kgf/cm", 0.0980665),
    ({LabParameter.ucs, LabParameter.tensile_strength, LabParameter.e_modulus}, r"^gpa$", 1000.0),
    ({LabParameter.cohesion, LabParameter.undrained_shear_strength}, r"^kg/?cm2|kgf/cm", 98.0665),
    ({LabParameter.cohesion, LabParameter.undrained_shear_strength}, r"^mpa$", 1000.0),
    ({LabParameter.unit_weight, LabParameter.dry_unit_weight}, r"^t/?m3|tn/?m3|g/?cm3|mg/m3", 9.80665),
    ({LabParameter.hydraulic_conductivity}, r"^cm/s", 0.01),
    ({LabParameter.hydraulic_conductivity}, r"^m/d(ay)?$", 1 / 86400),
]


def lab_qualifier(value_raw: str) -> str | None:
    """'<0.01' → '<', '5 - 10' → 'range': the stored number is then not an exact value."""
    m = re.match(r"\s*([<>≤≥])", value_raw or "")
    if m:
        return m.group(1)
    return "range" if re.search(r"\d\s*[-–~]\s*\d", value_raw or "") else None


def _lab_value(parameter: LabParameter, value_raw: str, unit_raw: str | None) -> float | None:
    value = _num(value_raw)
    if value is None:
        return None
    unit = (unit_raw or "").lower().replace(" ", "").replace("³", "3").replace("²", "2").strip("()[]")
    for params, pattern, factor in _LAB_FACTORS:
        if parameter in params and re.search(pattern, unit):
            return value * factor
    if not unit or unit == "-" or parameter in _DIMENSIONLESS:
        return value
    if any(parameter in params and re.search(pattern, unit) for params, pattern in _LAB_NATIVE):
        return value
    return None  # unknown unit: do not publish a number in the wrong unit


def _bbox(raw: list[float] | None) -> tuple[float, float, float, float] | None:
    """Model-reported region as fractions of the page image; anything implausible is dropped."""
    if not raw or len(raw) != 4:
        return None
    x0, y0, x1, y1 = raw
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        return None
    return (x0, y0, x1, y1)


def _same_page(a: SourceRef | None, b: SourceRef | None) -> bool:
    return a is not None and b is not None and (a.file, a.page) == (b.file, b.page)


CONTINUED = re.compile(r"\((?:continued|cont'?d\.?|cont\.|fortsetzung|continuaci[oó]n|continua[çc][aã]o)\)|\bcontinued\b(?!\w)|^\s*\.{3}", re.I)


def _continues(prev: Layer, nxt: Layer) -> bool:
    """A layer split by a sheet break: same boundary, next sheet, same lithology, and either an
    explicit continuation marker or the same description."""
    if _same_page(prev.source, nxt.source) or abs(prev.bottom_m - nxt.top_m) > 0.05 or prev.primary != nxt.primary:
        return False
    a = re.sub(r"\W+", " ", prev.description_original.lower()).strip()
    b = re.sub(r"\W+", " ", nxt.description_original.lower()).strip()
    return bool(CONTINUED.search(nxt.description_original)) or a == b


SHEET = re.compile(r"(\d{1,2})\s*(?:of|de|von|aus|di|sur|af|av|/)\s*(\d{1,2})", re.IGNORECASE)


def missing_sheets(logs: list[tuple[XLog, SourceRef]]) -> list[str]:
    """'missing sheets: 2 of 3' when the printed sheet indicators ('1 of 3', 'Hoja 3 de 3') leave a hole.
    Only numbering that plausibly counts this hole's sheets is checked (a total of at most 5, or at most as
    many missing as found): '5 of 40' is a sheet number of the whole report."""
    seen: set[int] = set()
    totals: Counter[int] = Counter()
    for log, _ in logs:
        m = SHEET.search(log.sheet or "")
        if m and 1 <= int(m.group(1)) <= int(m.group(2)) <= 50:
            seen.add(int(m.group(1)))
            totals[int(m.group(2))] += 1
    if not totals:
        return []
    total = totals.most_common(1)[0][0]
    missing = [k for k in range(1, total + 1) if k not in seen]
    # A small total counts this hole's sheets ('1 of 3'); a large one with many gaps is report pagination.
    if not missing or (len(missing) > len(seen) and total > 5):
        return []
    return [f"missing sheets: {', '.join(map(str, missing))} of {total}"]


def _layers(logs: list[tuple[XLog, SourceRef]], final_depth: float | None,
            notes: list[str] | None = None) -> list[Layer]:
    flat = [(x, src) for log, src in logs for x in log.layers]
    if notes is not None:
        notes.extend(missing_sheets(logs))
    # Fill missing boundaries from neighbours: top ← previous bottom, bottom ← next top / final depth.
    # Surface and final depth are assumptions, not readings: they are reported as findings.
    tops = [x.top_m for x, _ in flat]
    bottoms = [x.bottom_m for x, _ in flat]
    inferred: list[str] = []
    for i in range(len(flat)):
        if tops[i] is None:
            tops[i] = bottoms[i - 1] if i > 0 else 0.0
            if i == 0 and bottoms[i] is not None:
                inferred.append("boundary inferred: top of the first layer not readable, set to 0 m")
        if bottoms[i] is None:
            if i + 1 < len(flat):
                bottoms[i] = tops[i + 1]  # stays None if the next top is unknown too
            elif final_depth is not None and tops[i] is not None and final_depth <= tops[i] + 0.01:
                # The hole ends where this unit starts (e.g. a pit stopped on rock): a finding, not a layer.
                inferred.append(f"reached at the final depth {final_depth:g} m, thickness unknown: "
                                f"{' '.join(flat[i][0].description_original.split())[:80]}")
            else:
                bottoms[i] = final_depth  # only the last layer may close at the final depth
                if final_depth is not None and tops[i] is not None:
                    inferred.append(f"boundary inferred: bottom of the last layer not readable, "
                                    f"set to the final depth {final_depth:g} m")
    if notes is not None:
        notes.extend(inferred)
    layers: list[Layer] = []
    for index, ((x, src), top, bottom) in enumerate(zip(flat, tops, bottoms)):
        if top is None or bottom is None:
            continue
        if index == len(flat) - 1 and abs(bottom - top) < 0.01 and (final_depth is None or abs(final_depth - top) < 0.01):
            # The last row describes what was hit at the end (a pit or hand auger stopped on rock).
            if notes is not None:
                notes.append(f"reached at the final depth {top:g} m, thickness unknown: "
                             f"{' '.join(x.description_original.split())[:80]}")
            continue
        if (layers and not _same_page(src, layers[-1].source) and top < layers[-1].bottom_m < bottom
                and any(abs(l.top_m - top) < 0.05 and (l.primary == x.primary or Lithology.unknown in (l.primary, x.primary))
                        for l in layers)):  # a different material with the same top is a contradiction, not a restatement
            # A unit running over several sheets, restated with its full range on a later sheet:
            # keep only the part below what the earlier sheets already cover.
            if notes is not None:
                notes.append(f"layer {top:g}–{bottom:g} m restated on page {src.page}; "
                             f"top set to {layers[-1].bottom_m:g} m")
            top = layers[-1].bottom_m
        layer = Layer(
            top_m=top,
            bottom_m=bottom,
            description_original=x.description_original,
            description_en=x.description_en,
            primary=x.primary,
            secondary=[SecondaryComponent(lithology=s.lithology, qualifier=s.qualifier) for s in x.secondary],
            uscs=x.uscs,
            stratigraphic_name=x.stratigraphic_name,
            stratigraphic_code=x.stratigraphic_code,
            stratigraphic_color=x.stratigraphic_color,
            geologic_age=x.geologic_age,
            color=x.color,
            consistency=x.consistency,
            relative_density=x.relative_density,
            weathering=x.weathering,
            rock_strength=x.rock_strength,
            moisture=x.moisture,
            source=SourceRef(file=src.file, page=src.page, bbox=_bbox(x.bbox)),
        )
        prev = layers[-1] if layers else None
        if (prev and layer.primary == Lithology.unknown and not _same_page(src, prev.source)
                and abs(prev.bottom_m - top) < 0.05 and prev.primary not in (Lithology.unknown, Lithology.no_recovery)):
            # Same unit across a sheet break whose soil/rock name is printed on the earlier sheet only.
            layer.primary = prev.primary
            if notes is not None:
                notes.append(f"lithology of layer {top:g}–{bottom:g} m taken from the previous sheet")
        if layers and _continues(layers[-1], layer):
            layers[-1].bottom_m = layer.bottom_m  # continuation across a sheet break
            continue
        if (layers and abs(layers[-1].top_m - top) < 0.01 and abs(layers[-1].bottom_m - bottom) < 0.01
                and layers[-1].primary == layer.primary):
            continue  # same layer read twice (overlapping sheets); a different material stays as an overlap
        prev = layers[-1] if layers else None
        if (prev and not _same_page(src, prev.source) and abs(prev.bottom_m - bottom) < 0.01
                and prev.top_m < top < prev.bottom_m and prev.primary == layer.primary):
            # The earlier sheet gave the unit's full range, the next one repeats it from its sheet top.
            if notes is not None:
                notes.append(f"layer {top:g}–{bottom:g} m on page {src.page} repeats {prev.top_m:g}–{prev.bottom_m:g} m")
            continue
        if (prev and not _same_page(src, prev.source) and abs(prev.top_m - top) < 0.01
                and bottom < prev.bottom_m - 0.01):
            # The earlier sheet ran its last unit past its own end; the next sheet shows where it really ends.
            if notes is not None:
                notes.append(f"layer {prev.top_m:g}–{prev.bottom_m:g} m ends at {bottom:g} m per page {src.page}")
            layers[-1] = layer
            continue
        layers.append(layer)
    return layers


def extend_sampled_layers(bh: Borehole, sample_max: float = 0.6, gap_max: float = 1.6) -> list[str]:
    """Logs that describe the soil at each sample (SPT 0.45 m, then nothing until the next sample) leave
    gaps between samples. A short described interval followed by a gap of at most `gap_max` to the next
    layer is extended to it, as an inferred boundary (never released without a look)."""
    notes = []
    layers = sorted(bh.layers, key=lambda layer: layer.top_m)
    for a, b in zip(layers, layers[1:]):
        gap = b.top_m - a.bottom_m
        if a.top_m > 0 and a.bottom_m - a.top_m <= sample_max and 0.1 < gap <= gap_max:  # a top layer is no sample
            notes.append(f"boundary inferred: layer {a.top_m:g}–{a.bottom_m:g} m (a sample) extended to the next "
                         f"description at {b.top_m:g} m")
            a.bottom_m = b.top_m
    return notes


def fill_core_loss(bh: Borehole, tol: float = 0.05) -> list[str]:
    """A gap inside one core run with less than full recovery, no longer than the unrecovered length,
    is core loss: fill it with a no_recovery layer and say so."""
    notes = []
    budget: dict[int, float] = {}  # unrecovered length left per core run: two gaps cannot spend it twice
    layers = sorted(bh.layers, key=lambda layer: layer.top_m)
    for a, b in zip(layers, layers[1:]):
        gap = b.top_m - a.bottom_m
        if gap <= tol:
            continue
        run = next((c for c in bh.core_runs if c.top_m - tol <= a.bottom_m and b.top_m <= c.bottom_m + tol), None)
        tcr = run.total_core_recovery_pct if run else None
        if tcr is None or tcr >= 100:
            continue
        left = budget.setdefault(id(run), (1 - tcr / 100) * (run.bottom_m - run.top_m))
        if gap > left + tol:
            continue
        budget[id(run)] = left - gap
        bh.layers.append(Layer(top_m=a.bottom_m, bottom_m=b.top_m, description_original="core loss (inferred)",
                               primary=Lithology.no_recovery, source=a.source))
        notes.append(f"core loss inferred: {a.bottom_m:g}–{b.top_m:g} m in core run {run.top_m:g}–{run.bottom_m:g} m "
                     f"(TCR {tcr:g} %)")
    bh.layers.sort(key=lambda layer: layer.top_m)
    return notes


# Explicit coordinate system statements in free text (report prose, notes, legends). Weak hints such as a
# bare country name are left out on purpose: they would turn a guess into a "stated" system.
CRS_STATEMENT = re.compile(
    r"(epsg\s*:?\s*\d{4,5}|mga\s?(?:94|2020)[^\n]{0,24}|zone\s?\d{1,2}\s?[a-z]?\s?mga\s?(?:94|2020)|"
    r"utm[^\n]{0,16}?\d{1,2}\s?[a-z]?\b|osgb\s?36|british national grid|mt\.?\s?eden[^\n]{0,10}|nztm|"
    r"etrs\s?89[^\n]{0,20}|lv95|ch1903\+?|s34[sj]|s45b|system\s?34[^\n]{0,12}|rd\s?new|lambert.?93|kgd\s?2002[^\n]{0,20}|(?:east|central|west)\s?belt|"
    r"[동중서]부\s?원점|동해\s?원점|posgar[^\n]{0,16}|sirgas[^\n]{0,24})",
    re.I,
)


def document_crs(pages: list[PageResult]) -> tuple[str, int] | None:
    """The coordinate system a document states most often (logs, tables, page notes, text layer), as
    (statement, page). Only statements that resolve to an EPSG code count."""
    found: list[tuple[str, int]] = []
    for r in pages:
        if r.extraction:
            found += [(log.crs_raw, r.page) for log in r.extraction.logs if log.crs_raw]
            found += [(row.crs_raw, r.page) for row in r.extraction.coordinate_rows if row.crs_raw]
        if r.crs_text:
            found.append((r.crs_text, r.page))
        found += [(m.group(0), r.page) for m in CRS_STATEMENT.finditer(r.text or "")]
    resolved = [(tuple(epsgs), text, page) for text, page in found if (epsgs := crs_from_hint(text))]
    if not resolved:
        return None
    # Count pages, not mentions (a table repeats its header on every row), and refuse to choose when the
    # document names different systems with comparable support.
    support = Counter(epsgs for epsgs, _ in {(e, p) for e, _, p in resolved})
    (top, n), *rest = support.most_common()
    if rest and rest[0][1] * 2 >= n:
        return None
    _, text, page = next(x for x in resolved if x[0] == top)
    return " ".join(text.split()), page


def flag_position_outliers(boreholes: list[Borehole]) -> None:
    """Holes of one document lie close together; one far from the others hints at a misread or a wrong CRS."""
    precise = [b for b in boreholes if b.location.lat is not None
               and b.location.method in (GeorefMethod.stated_coordinates, GeorefMethod.linked_table)]
    if len(precise) < 3:
        return
    lat = statistics.median(b.location.lat for b in precise)
    lon = statistics.median(b.location.lon for b in precise)
    dist = {b.borehole_id: haversine_km(b.location.lat, b.location.lon, lat, lon) for b in precise}
    typical = statistics.median(dist.values())
    for b in precise:
        if dist[b.borehole_id] > max(20.0, 5 * typical):
            b.issues.append(f"position {dist[b.borehole_id]:.0f} km from the other holes of this document")
            b.location.confidence = min(b.location.confidence or 0.0, 0.5)


def _unique_groundwater(observations) -> list[GroundwaterObservation]:
    """Header fields are repeated on every sheet; keep one entry per distinct observation."""
    seen: dict[tuple, GroundwaterObservation] = {}
    for g in observations:
        seen.setdefault((g.depth_m, g.kind, g.reference, g.dry, g.observed_on), g)
    return list(seen.values())


FEET = re.compile(r"\b(?:ft|feet|foot)\b|^\s*'\s*$", re.I)


def unit_findings(xs: list[XLog]) -> list[str]:
    """The model converts feet to metres itself; a person checks that conversion once."""
    units = {x.depth_unit_raw.strip() for x in xs if x.depth_unit_raw and FEET.search(x.depth_unit_raw)}
    return [f"depths printed in feet ({', '.join(sorted(units))}); check the conversion to metres"] if units else []


def _first(values):
    return next((v for v in values if v not in (None, "", [])), None)


# Model artefacts in a location text: "(from text file path ...)", "(see file name ...)", "(reports/x.pdf)".
_SOURCE_NOTE = re.compile(
    r"\s*\((?:[^()]*\bfile(?:name)?\b[^()]*|[^()]*\.(?:pdf|png|jpe?g|tiff?|txt|docx?|xlsx?)\b[^()]*)\)",
    re.IGNORECASE,
)


def clean_location_text(text: str | None) -> str | None:
    """A printed place name without notes about where the model read it; None if nothing is left."""
    if not text:
        return None
    cleaned = " ".join(_SOURCE_NOTE.sub("", text).split()).strip(" ,;-")
    return cleaned or None


def build_borehole(doc_id: str, source_file: str, logs: list[tuple[XLog, SourceRef]],
                   coord_rows: dict[str, XCoordinateRow], doc_place: tuple[float, float, float] | None,
                   doc_crs: tuple[str, int] | None = None, kind: str | None = None) -> tuple[Borehole, int]:
    """The record (not yet scored) and the number of items the model reported as unreadable.
    coord_rows is keyed by hole_key; kind is the hole type of the group (borehole, test_pit, probe)."""
    xs = [log for log, _ in logs]
    name = xs[0].name
    key = hole_key(name, kind)
    # Earlier sheets often state where the sheet ends; the hole's final depth is the deepest one stated.
    final_depth = max((x.final_depth_m for x in xs if x.final_depth_m is not None), default=None)
    layer_notes: list[str] = []

    coords_raw = _first(x.coords_raw for x in xs)
    crs_raw = _first(x.crs_raw for x in xs)
    method = GeorefMethod.stated_coordinates
    row = lookup(coord_rows, key)
    if row and not coords_raw:
        coords_raw, crs_raw, method = row.coords_raw, row.crs_raw or crs_raw, GeorefMethod.linked_table
    from_document = bool(coords_raw and not crs_raw and doc_crs)
    if from_document:
        crs_raw = doc_crs[0]
    country = _first(x.country for x in xs)
    location_name = _first(clean_location_text(x.location_text) for x in xs)
    # The model's estimate of the named site differs per sheet (often just the country centroid on one
    # sheet and the town on the next): use the most precise one of this hole or of the document.
    own = [(x.place_lat, x.place_lon, x.place_precision_km or 10.0) for x in xs
           if x.place_lat is not None and x.place_lon is not None]
    place = min(own + ([doc_place] if doc_place else []), key=lambda p: p[2], default=None)
    geo = georeference(coords_raw, crs_raw, country, *(place or (None, None, None)),
                       place_name=location_name)
    location = geo.location
    if location.method == GeorefMethod.stated_coordinates and method == GeorefMethod.linked_table:
        location.method = method
        location.is_planned = row.is_planned if row else None
    if from_document:
        location.crs_raw = None  # not printed on this log
        if location.crs_epsg in crs_from_hint(doc_crs[0]):
            geo.issues.append(f"coordinate system taken from page {doc_crs[1]} of the document: {doc_crs[0]}")
            location.confidence = min(location.confidence or 0.0, 0.9)  # stated, but not on this log
    coords_log = next(((x, src) for x, src in logs if x.coords_raw), None)
    location.source = SourceRef(file=coords_log[1].file, page=coords_log[1].page, bbox=_bbox(coords_log[0].coords_bbox)) \
        if coords_log else logs[0][1]

    itype = _first(x.investigation_type for x in xs) or key[0]
    elevation_raw = _first(x.elevation_raw for x in xs) or (row.elevation_raw if row else None)
    bh = Borehole(
        borehole_id=f"{doc_id}:{id_key(name)}",
        borehole_name=name,
        investigation_type=InvestigationType(itype) if itype in ("borehole", "test_pit") else (
            InvestigationType.other if itype else None),
        location_name=location_name,
        project_id=_first(x.project_id for x in xs),
        project_name=_first(x.project_name for x in xs),
        client=_first(x.client for x in xs),
        contractor=_first(x.contractor for x in xs),
        start_date=_date(_first(x.start_date for x in xs)),
        end_date=_date(_first(x.end_date for x in xs)),
        location=location,
        surface_elevation_m=_first(x.elevation_m for x in xs),
        surface_elevation_raw=elevation_raw,
        elevation_datum=_first(x.elevation_datum for x in xs),
        depth_m=final_depth,
        length_m=_first(x.length_m for x in xs),
        inclination_deg=_first(x.inclination_deg for x in xs),
        drilling_method=_first(x.drilling_method for x in xs),
        notes="\n".join(dict.fromkeys(x.notes for x in xs if x.notes)) or None,
        layers=_layers(logs, final_depth, layer_notes),
        core_runs=[CoreRun(top_m=c.top_m, bottom_m=c.bottom_m, total_core_recovery_pct=c.tcr_pct,
                           solid_core_recovery_pct=c.scr_pct, rqd_pct=c.rqd_pct,
                           fracture_frequency_per_m=c.fracture_frequency_per_m, source=src)
                   for x, src in logs for c in x.core_runs if c.top_m is not None and c.bottom_m is not None],
        samples=[Sample(name=s.name, top_m=s.top_m, bottom_m=s.bottom_m, type=s.type, type_raw=s.type_raw, source=src)
                 for x, src in logs for s in x.samples if s.top_m is not None],
        spt=[SPT(depth_m=s.depth_m, blows=s.blows, increment_mm=s.increment_mm, n_value=s.n_value,
                 refusal=bool(s.refusal), raw=s.raw, source=src)
             for x, src in logs for s in x.spt if s.depth_m is not None],
        groundwater=_unique_groundwater(
            GroundwaterObservation(depth_m=g.depth_m, kind=g.kind, observed_on=_date(g.date), dry=g.dry,
                                   reference=g.reference, raw=g.raw, source=src)
            for x, src in logs for g in x.groundwater),
        lab_results=[LabResult(parameter=lab.parameter, parameter_raw=lab.parameter_raw, value=v,
                               value_raw=f"{lab.value_raw} {lab.unit_raw or ''}".strip(), top_m=lab.top_m,
                               bottom_m=lab.bottom_m, sample_name=lab.sample_name, source=src,
                               qualifier=lab_qualifier(lab.value_raw))
                     for x, src in logs for lab in x.lab_results
                     if (v := _lab_value(lab.parameter, lab.value_raw, lab.unit_raw)) is not None],
        sources=[src for _, src in logs],
    )
    layer_notes += fill_core_loss(bh)
    layer_notes += extend_sampled_layers(bh)
    issues = bh.validate_log() + layer_notes + geo.issues + unit_findings(xs)
    unreadable = [u for x in xs for u in x.unreadable]
    issues += [f"unreadable: {u}" for u in unreadable[:5]]
    bh.issues = list(dict.fromkeys(issues))
    return bh, len(unreadable)


def assemble(results: list[PageResult]) -> list[Borehole]:
    by_doc: dict[str, list[PageResult]] = defaultdict(list)
    for r in results:
        by_doc[r.doc_id].append(r)

    # Folders of single images (e.g. one JPG per hole) form one document for CRS and position checks.
    by_group: dict[str, list[PageResult]] = defaultdict(list)
    for r in results:
        by_group[r.doc_id.split("__")[0]].append(r)
    group_crs = {g: document_crs(rs) for g, rs in by_group.items()}

    boreholes: list[Borehole] = []
    built: dict[str, list[tuple[Borehole, int, dict]]] = defaultdict(list)
    for doc_id, pages in by_doc.items():
        group = doc_id.split("__")[0]
        pages.sort(key=lambda r: r.page)
        coord_rows: dict[str, XCoordinateRow] = {}
        groups: dict[str, list[tuple[XLog, SourceRef]]] = defaultdict(list)
        places = []
        page_texts = {(r.source_file, r.page): r.text for r in pages}
        for r in pages:
            if r.extraction is None:
                continue
            for row in r.extraction.coordinate_rows:
                k = hole_key(row.name, None)
                if k not in coord_rows or (coord_rows[k].is_planned and not row.is_planned):
                    coord_rows[k] = row
            for log in r.extraction.logs:
                if r.kind not in LOG_KINDS and log.layers:
                    # A summary or coordinate table ('depth to rock: 40 m') is no layer sequence: it adds
                    # coordinates and header values to the hole, not layers.
                    log = log.model_copy(update={"layers": []})
                if log.layers or log.coords_raw or r.kind in LOG_KINDS:
                    k = hole_key(log.name, log.investigation_type if log.investigation_type in ("borehole", "test_pit", "probe")
                                 else _KINDS.get(r.kind))
                    groups[k].append((log, SourceRef(file=r.source_file, page=r.page)))
                if log.place_lat is not None and log.place_lon is not None:
                    places.append((log.place_lat, log.place_lon, log.place_precision_km or 10.0))
        doc_place = None
        if places:
            # Median of the more precise estimates only, so a few country-level guesses don't drag it away.
            best = min(p[2] for p in places)
            precise = [p for p in places if p[2] <= max(3 * best, 10.0)]
            doc_place = (statistics.median(p[0] for p in precise), statistics.median(p[1] for p in precise),
                         statistics.median(p[2] for p in precise))
        doc_records: list[Borehole] = []
        for (kind, _), logs in groups.items():
            if any(log.layers for log, _ in logs):
                try:
                    bh, unreadable = build_borehole(doc_id, pages[0].source_file, logs, coord_rows, doc_place,
                                                    group_crs[group], kind)
                    built[group].append((bh, unreadable, page_texts))
                    doc_records.append(bh)
                except Exception as exc:  # one broken record must not stop the whole run
                    print(f"  ! could not assemble {doc_id}:{logs[0][0].name}: {type(exc).__name__}: {exc}")
        _unique_ids(doc_records)
        _unique_ids_by_type(doc_records)
    for group, records in built.items():
        linked, unmatched, implausible = link_lab_results([bh for bh, _, _ in records], by_group[group])
        if unmatched or implausible:
            print(f"  {group}: {linked} lab results linked, {unmatched} without a matching hole, "
                  f"{implausible} outside plausible ranges")
        flag_position_outliers([bh for bh, _, _ in records])
        for bh, unreadable, page_texts in records:
            score(bh, page_texts, unreadable)
            boreholes.append(bh)
    return dedupe(boreholes)


def dedupe(boreholes: list[Borehole]) -> list[Borehole]:
    """Same hole in several documents (e.g. a PDF and its PNG export): keep the richer record."""
    kept: dict[tuple, Borehole] = {}
    for bh in boreholes:
        loc = bh.location
        precise = loc.lat is not None and loc.method in (GeorefMethod.stated_coordinates, GeorefMethod.linked_table)
        if not precise:
            kept[(bh.borehole_id,)] = bh  # never merge on approximate positions
            continue
        project = re.sub(r"\W+", "", (bh.project_name or "").lower())
        key = (norm_name(bh.borehole_name), project, round(loc.lat, 4), round(loc.lon, 4))
        other = kept.get(key)
        if other is None:
            kept[key] = bh
            continue
        # The better-checked record wins; the layer count only breaks ties (more layers can mean over-segmented).
        better = lambda b: (b.confidence or 0.0, len(b.layers))
        richer, poorer = (bh, other) if better(bh) > better(other) else (other, bh)
        richer.sources = list({(s.file, s.page): s for s in richer.sources + poorer.sources}.values())
        kept[key] = richer
    return list(kept.values())


def _same_value(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 + 5e-4 * max(abs(a), abs(b))


# Physically plausible ranges in the stored units: a value outside is a misread or a unit error
# (e.g. UCS '9100' without unit is kPa, not MPa) and is not published.
LAB_RANGES: dict[LabParameter, tuple[float, float]] = {
    LabParameter.ucs: (0.05, 500), LabParameter.tensile_strength: (0.01, 100), LabParameter.point_load_index: (0.01, 25),
    LabParameter.e_modulus: (0.1, 200_000), LabParameter.cai: (0, 8), LabParameter.water_content: (0, 500),
    LabParameter.unit_weight: (8, 35), LabParameter.dry_unit_weight: (5, 32), LabParameter.fines: (0, 100),
    LabParameter.sand: (0, 100), LabParameter.gravel: (0, 100), LabParameter.liquid_limit: (0, 500),
    LabParameter.plastic_limit: (0, 300), LabParameter.plasticity_index: (0, 300), LabParameter.friction_angle: (0, 60),
    LabParameter.cohesion: (0, 5000), LabParameter.undrained_shear_strength: (0, 5000),
    LabParameter.hydraulic_conductivity: (1e-14, 1), LabParameter.poissons_ratio: (0, 0.5), LabParameter.rmr: (0, 100),
    LabParameter.gsi: (0, 100), LabParameter.q: (1e-4, 1000),
}


def plausible(parameter: LabParameter, value: float) -> bool:
    lo, hi = LAB_RANGES.get(parameter, (float("-inf"), float("inf")))
    return lo <= value <= hi


def link_lab_results(records: list[Borehole], pages: list[PageResult]) -> tuple[int, int, int]:
    """Attach the rows of lab sheets to the holes of the same document: by hole name (also with a test
    suffix such as 'QX 16 - FHT'), else by a sample name that is a hole name or that only one hole has,
    else to the only hole of the document. Returns (linked, unmatched, implausible)."""
    by_name: dict[tuple[str | None, str], Borehole] = {}
    for bh in records:
        by_name.setdefault(hole_key(bh.borehole_name, bh.investigation_type.value if bh.investigation_type else None), bh)
    by_sample: dict[str, list[tuple[Borehole, object]]] = defaultdict(list)
    for bh in records:
        for sample in bh.samples:
            if sample.name:
                by_sample[norm_name(sample.name)].append((bh, sample))
    def deep_enough(bh: Borehole, depth: float | None) -> bool:
        end = bh.depth_m or max((layer.bottom_m for layer in bh.layers), default=None)
        return depth is None or end is None or depth <= end + 0.5

    def find_hole(name: str, page_kind: str | None, depth: float | None) -> Borehole | None:
        kind, number = hole_key(name, page_kind)
        bh = lookup(by_name, (kind, number))
        if bh is None and kind is None:  # same number for a borehole and a pit: the depth decides
            fits = [b for (k, n), b in by_name.items() if n == number and deep_enough(b, depth)]
            bh = fits[0] if len(fits) == 1 else None
        if bh is None:
            # 'QX10CONTAMLAND', 'QX16FHT': a known hole followed by a word. A single letter ('QX11A') is
            # another hole (a re-drill) and does not match.
            prefixes = [(n, b) for (k, n), b in by_name.items() if n[-1:].isdigit() and number.startswith(n)
                        and len(number) - len(n) >= 2 and number[len(n)].isalpha() and (kind is None or k in (kind, None))]
            bh = max(prefixes, key=lambda nb: len(nb[0]))[1] if prefixes else None
        return bh

    linked = unmatched = implausible = 0
    for r in pages:
        if r.labs is None:
            continue
        # A sheet that names the type for some rows ('T.P. 3') uses it for the rows that only give the number.
        kinds = {hole_key(row.hole_name, None)[0] for row in r.labs.rows if row.hole_name} - {None}
        page_kind = kinds.pop() if len(kinds) == 1 else None
        for row in r.labs.rows:
            value = _lab_value(row.parameter, row.value_raw, row.unit_raw)
            if value is None:
                continue  # no number (e.g. 'NP', '-')
            if not plausible(row.parameter, value):
                implausible += 1
                continue
            bh = None
            if row.hole_name:
                bh = find_hole(row.hole_name, page_kind, row.top_m)
                if bh is not None and not deep_enough(bh, row.top_m):
                    bh = None
            elif row.sample_name:  # sheets often put the hole id into the sample column
                bh = find_hole(row.sample_name, page_kind, row.top_m)
            sample = None
            if row.sample_name:
                hits = by_sample.get(norm_name(row.sample_name), [])
                if bh is not None:
                    sample = next((s for b, s in hits if b is bh), None)
                elif len(hits) == 1:
                    bh, sample = hits[0]
            if bh is None and len(records) == 1 and not row.hole_name:
                bh = records[0]
            if bh is None:
                unmatched += 1
                continue
            top = row.top_m if row.top_m is not None else (sample.top_m if sample else None)
            bottom = row.bottom_m if row.bottom_m is not None else (sample.bottom_m if sample and row.top_m is None else None)
            result = LabResult(parameter=row.parameter, parameter_raw=row.parameter_raw, value=value,
                               value_raw=f"{row.value_raw} {row.unit_raw or ''}".strip(), top_m=top, bottom_m=bottom,
                               sample_name=row.sample_name, source=SourceRef(file=r.source_file, page=r.page),
                               qualifier=lab_qualifier(row.value_raw))
            # The same test is often on the log and on the lab sheet (only the sheet has the sample id).
            same = next((x for x in bh.lab_results if x.parameter == result.parameter
                         and _same_value(x.value, result.value) and x.top_m == result.top_m), None)
            if same is None:
                bh.lab_results.append(result)
            elif same.sample_name is None:
                same.sample_name = result.sample_name
            linked += 1
    return linked, unmatched, implausible
