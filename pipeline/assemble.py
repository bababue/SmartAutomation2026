"""Turn per-page extractions into canonical `Borehole` records.

- Sheets of the same hole within a document are merged (continuation layers joined).
- A sheet that prints another position, ground level or investigation (report number, project, date) than
  the earlier sheets of its name goes to review, and is a hole of its own when its depths restart at the surface.
- Coordinates from summary tables are linked to logs by hole name.
- A coordinate system stated elsewhere in the document (other holes, tables, notes, report text) is used
  when a log does not name one; positions far from the other holes of a document are flagged.
- Units are normalised here, in code, never by the model.
- The same hole in several documents (a log, its image export, a drawing) becomes one record: header values
  are filled across them, and disagreements or uncertain matches go to review.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import date
from itertools import combinations
from typing import TypeVar

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
    ReviewStatus,
    Sample,
    SecondaryComponent,
    Lithology,
    SourceRef,
)

from .extraction_models import PageExtraction, PageLabResults, XCoordinateRow, XLab, XLabRow, XLog
from . import corrections, store
from .georef import candidates, crs_from_hint, georeference, haversine_km, is_precise
from .knowledge import printed_pair
from .scoring import SOURCE_CONFLICTS, STRUCTURAL, counts_against, finding_severity, score

LOG_KINDS = {"borehole_log", "test_pit_log", "probe_log"}
HoleKey = tuple[str | None, str]  # (type, number), see hole_key
T = TypeVar("T")


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
            bh.borehole_id = f"{document_id(bh)}:{id_key(bh.borehole_name, separate_numbers=True)}"


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
_HOLE_KIND_OF_PAGE = {"test_pit_log": "test_pit", "probe_log": "probe", "borehole_log": "borehole"}


def hole_key(name: str, kind: str | None) -> HoleKey:
    """(type, number) for matching logs, tables and lab rows: 'Test Pit No. 2' and 'T.P. 2' are the test pit
    '2', 'BH-2' is the borehole '2'. `kind` (from the page or the model) applies when the name has no type word."""
    for words, word_kind in ((_PIT_WORDS, "test_pit"), (_PROBE_WORDS, "probe"), (_HOLE_WORDS, "borehole")):
        rest = words.sub("", name, count=1)
        if rest != name and re.search(r"\d", rest):
            return word_kind, norm_name(rest)
    return (kind if kind in ("borehole", "test_pit", "probe") else None), norm_name(name)


def lookup_hole(index: dict[HoleKey, T], key: HoleKey) -> T | None:
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


_STRENGTH = {LabParameter.ucs, LabParameter.tensile_strength, LabParameter.point_load_index, LabParameter.e_modulus}
_SHEAR = {LabParameter.cohesion, LabParameter.undrained_shear_strength}
_WEIGHT = {LabParameter.unit_weight, LabParameter.dry_unit_weight}
# Units each parameter is stored in, as printed without conversion (see LAB_UNITS); a value with any
# other unit that has no factor below is dropped rather than published in the wrong unit.
_LAB_NATIVE: list[tuple[set[LabParameter], str]] = [
    (_STRENGTH, r"^(mpa|n/mm2)$"),
    (_SHEAR, r"^(kpa|kn/m2)$"),
    (_WEIGHT, r"^kn/m3$"),
    ({LabParameter.water_content, LabParameter.consistency, LabParameter.fines, LabParameter.sand, LabParameter.gravel,
      LabParameter.liquid_limit, LabParameter.plastic_limit}, r"^(%|percent)$"),
    ({LabParameter.friction_angle}, r"^(°|deg|degrees?)$"),
    ({LabParameter.hydraulic_conductivity}, r"^m/s(ec)?$"),
]
_DIMENSIONLESS = {LabParameter.cai, LabParameter.rmr, LabParameter.q, LabParameter.gsi, LabParameter.plasticity_index,
                  LabParameter.consistency_index, LabParameter.poissons_ratio, LabParameter.other}

# (parameters, unit pattern, factor to the target unit); the first match converts.
_LAB_FACTORS: list[tuple[set[LabParameter], str, float]] = [
    (_STRENGTH, r"^psi$", 0.00689476),
    (_STRENGTH, r"^ksi$", 6.89476),
    (_STRENGTH, r"^kn/m2$", 0.001),
    (_SHEAR, r"^psi$", 6.89476),
    (_SHEAR, r"^t/?m2$", 9.80665),
    (_WEIGHT, r"^pcf$|^lb/ft3$", 0.157087),
    (_STRENGTH, r"^kpa$", 0.001),
    (_STRENGTH, r"^kg/?cm2|kgf/cm", 0.0980665),
    ({LabParameter.ucs, LabParameter.tensile_strength, LabParameter.e_modulus}, r"^gpa$", 1000.0),
    (_SHEAR, r"^kg/?cm2|kgf/cm", 98.0665),
    (_SHEAR, r"^mpa$", 1000.0),
    (_WEIGHT, r"^t/?m3|tn/?m3|g/?cm3|mg/m3", 9.80665),
    ({LabParameter.hydraulic_conductivity}, r"^cm/s", 0.01),
    ({LabParameter.hydraulic_conductivity}, r"^m/d(ay)?$", 1 / 86400),
]

_NUMBER = r"[-+]?\d+(?:[.,]\d+)?(?:\s*(?:[eE]|[x×*]\s*10\^?)\s*[-+−]?\d+)?"


def lab_qualifier(value_raw: str) -> str | None:
    """'<0.01' → '<', '5 - 10' → 'range': the stored number is then not an exact value."""
    m = re.match(r"\s*([<>≤≥])", value_raw or "")
    if m:
        return m.group(1)
    return "range" if re.fullmatch(rf"\s*{_NUMBER}\s*[-–~]\s*{_NUMBER}\s*", value_raw or "") else None


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


def lab_upper(parameter: LabParameter, value_raw: str, unit_raw: str | None) -> float | None:
    match = re.fullmatch(rf"\s*{_NUMBER}\s*[-–~]\s*({_NUMBER})\s*", value_raw)
    return _lab_value(parameter, match.group(1), unit_raw) if match else None


def _lab_result(row: XLab | XLabRow, value: float, top_m: float | None, bottom_m: float | None,
                source: SourceRef) -> LabResult:
    return LabResult(parameter=row.parameter, parameter_raw=row.parameter_raw, value=value,
                     value_raw=f"{row.value_raw} {row.unit_raw or ''}".strip(), top_m=top_m, bottom_m=bottom_m,
                     sample_name=row.sample_name, source=source, qualifier=lab_qualifier(row.value_raw),
                     value_upper=lab_upper(row.parameter, row.value_raw, row.unit_raw))


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


# Text that points back at the previous sheet's unit instead of describing one: 'strata as previous page',
# 'As above', 'Ditto', '(continued from previous sheet: ...)'. A ditto further into a text refers to that text.
SAME_AS_BEFORE = re.compile(
    r"^\W*(?:(?:strata|stratum|same)\s+)?(?:as\s+(?:on\s+)?(?:the\s+)?(?:previous|preceding|last)\s+(?:page|sheet)"
    r"|as\s+above|ditto)\b"
    r"|continued\s+from\s+(?:the\s+)?(?:previous|preceding|last|above)(?:\s+(?:page|sheet))?|continuation\s+of\b"
    r"|^\s*\.{3}", re.I)

# Sheets of cable-percussive logs end at a round depth, but the last unit is often drawn down to the end of
# the last sample (10.45 m on a 10 m sheet).
SHEET_JOIN_M = 0.5


def _plain(text: str) -> str:
    return re.sub(r"\W+", " ", CONTINUED.sub(" ", SAME_AS_BEFORE.sub(" ", text)).lower()).strip()


PROPERTIES = sorted(set(Layer.model_fields) - {"top_m", "bottom_m", "description_original", "description_en",
                                               "source"})


def _same_properties(a: Layer, b: Layer) -> bool:
    return all(getattr(a, field) == getattr(b, field) for field in PROPERTIES)


def _carry_properties(prev: Layer, nxt: Layer) -> list[str]:
    """Take what a continuation sheet says about a unit into it: empty fields are filled and secondary components
    joined. A field both sheets fill differently is returned as a conflict."""
    conflicts = []
    for field in PROPERTIES:
        a, b = getattr(prev, field), getattr(nxt, field)
        if field == "primary" or b is None or a == b:
            continue
        if field == "secondary":
            known = {s.lithology for s in a}
            prev.secondary = a + [s for s in b if s.lithology not in known]
        elif a is None:
            setattr(prev, field, b)
        else:
            conflicts.append(f"{field} '{a}' on page {prev.source.page if prev.source else '?'}, "
                             f"'{b}' on page {nxt.source.page if nxt.source else '?'}")
    return conflicts


def _sheet_join(prev: Layer, nxt: Layer) -> str | None:
    """How the first layer of a sheet relates to the last one of the sheet before. 'merge': one unit split by
    the sheet break (a back-reference, the same description, or the same lithology run past the sheet end).
    'extend': a unit marked as continued, with its own description, below a short undescribed stretch: it
    started there, at the foot of the previous sheet. None: separate units, or too unclear to join."""
    if _same_page(prev.source, nxt.source) or nxt.top_m < prev.top_m:
        return None
    d = prev.bottom_m - nxt.top_m  # > 0: the sheets overlap; < 0: a stretch nobody describes
    if abs(d) > SHEET_JOIN_M:
        return None
    back = bool(SAME_AS_BEFORE.search(nxt.description_original))
    same_text = _plain(prev.description_original) == _plain(nxt.description_original)
    # The same text or a back-reference names one unit: properties read differently on each sheet (age, strength,
    # secondary components) are no boundary. Under another text they keep the units apart.
    if not (back or same_text) and not _same_properties(prev, nxt.model_copy(update={"primary": prev.primary})):
        return None
    marked = back or bool(CONTINUED.search(nxt.description_original))
    if prev.primary != nxt.primary and not (back and nxt.primary == Lithology.unknown):
        return "extend" if marked and not back and d < -0.05 else None
    if back or same_text:
        return "merge"
    if abs(d) <= 0.05:
        return "merge" if marked or nxt.bottom_m - nxt.top_m < 0.01 else None  # or a fragment without thickness
    if d > 0:
        return "merge"
    return "extend" if marked else None


SHEET = re.compile(r"(\d{1,2})\s*(?:of|de|von|aus|di|sur|af|av|/)\s*(\d{1,2})", re.IGNORECASE)


def _sheet_number(log: XLog) -> tuple[int, int] | None:
    """(k, n) of a sheet indicator 'k of n'."""
    m = SHEET.search(log.sheet or "")
    return (int(m.group(1)), int(m.group(2))) if m and 1 <= int(m.group(1)) <= int(m.group(2)) <= 50 else None


def missing_sheets(logs: list[tuple[XLog, SourceRef]]) -> list[str]:
    """'missing sheets: 2 of 3' when the printed sheet indicators ('1 of 3', 'Hoja 3 de 3') leave a hole.
    Only numbering that plausibly counts this hole's sheets is checked (a total of at most 5, or at most as
    many missing as found): '5 of 40' is a sheet number of the whole report."""
    seen: set[int] = set()
    totals: Counter[int] = Counter()
    for log, _ in logs:
        if sheet := _sheet_number(log):
            seen.add(sheet[0])
            totals[sheet[1]] += 1
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
    """The layer sequence of a hole's sheets; findings about how it was read are added to `notes`."""
    notes = [] if notes is None else notes
    flat = [(x, src) for log, src in logs for x in log.layers]
    notes.extend(missing_sheets(logs))
    # Fill missing boundaries from neighbours: top ← previous bottom, bottom ← next top / final depth.
    # Surface and final depth are assumptions, not readings: they are reported as findings.
    tops = [x.top_m for x, _ in flat]
    bottoms = [x.bottom_m for x, _ in flat]
    for i in range(len(flat)):
        if tops[i] is None:
            tops[i] = bottoms[i - 1] if i > 0 else 0.0
            if i == 0 and bottoms[i] is not None:
                notes.append("boundary inferred: top of the first layer not readable, set to 0 m")
        if bottoms[i] is None:
            if i + 1 < len(flat):
                bottoms[i] = tops[i + 1]  # stays None if the next top is unknown too
            elif final_depth is not None and tops[i] is not None and final_depth <= tops[i] + 0.01:
                # The hole ends where this unit starts (e.g. a pit stopped on rock): a finding, not a layer.
                notes.append(f"reached at the final depth {final_depth:g} m, thickness unknown: "
                             f"{' '.join(flat[i][0].description_original.split())[:80]}")
            else:
                bottoms[i] = final_depth  # only the last layer may close at the final depth
                if final_depth is not None and tops[i] is not None:
                    notes.append(f"boundary inferred: bottom of the last layer not readable, "
                                 f"set to the final depth {final_depth:g} m")
    layers: list[Layer] = []
    for index, ((x, src), top, bottom) in enumerate(zip(flat, tops, bottoms)):
        if top is None or bottom is None:
            continue
        if index == len(flat) - 1 and abs(bottom - top) < 0.01 and (final_depth is None or abs(final_depth - top) < 0.01):
            # The last row describes what was hit at the end (a pit or hand auger stopped on rock).
            notes.append(f"reached at the final depth {top:g} m, thickness unknown: "
                         f"{' '.join(x.description_original.split())[:80]}")
            continue
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
        new_sheet = index > 0 and not _same_page(src, flat[index - 1][1])
        if not _join_into(layers, layer, new_sheet, notes):
            layers.append(layer)
    return layers


def _join_into(layers: list[Layer], layer: Layer, new_sheet: bool, notes: list[str]) -> bool:
    """Fit `layer` to the layers read so far across a sheet break: it may restate, continue or end a unit of the
    previous sheet. Returns whether it was taken in that way; otherwise it is a layer of its own."""
    src, top, bottom = layer.source, layer.top_m, layer.bottom_m
    if (layers and not _same_page(src, layers[-1].source) and top < layers[-1].bottom_m < bottom
            and any(abs(l.top_m - top) < 0.05 for l in layers)
            and all(_same_properties(l, layer) for l in layers if l.bottom_m > top and l.top_m < bottom)):
        notes.append(f"layer {top:g}–{bottom:g} m restated on page {src.page}; top set to {layers[-1].bottom_m:g} m")
        layer.top_m = top = layers[-1].bottom_m
    prev = layers[-1] if layers else None
    if (prev and layer.primary == Lithology.unknown and not _same_page(src, prev.source)
            and abs(prev.bottom_m - top) < 0.05 and prev.primary not in (Lithology.unknown, Lithology.no_recovery)):
        # Same unit across a sheet break whose soil/rock name is printed on the earlier sheet only.
        layer.primary = prev.primary
        notes.append(f"lithology of layer {top:g}–{bottom:g} m taken from the previous sheet")
    join = _sheet_join(prev, layer) if prev and new_sheet else None
    if join == "merge":
        if abs(prev.bottom_m - top) > 0.05:
            notes.append(f"layer {top:g}–{bottom:g} m on page {src.page} repeats the unit "
                         f"{prev.top_m:g}–{prev.bottom_m:g} m of the previous sheet; joined as "
                         f"{prev.top_m:g}–{max(prev.bottom_m, bottom):g} m")
        prev.bottom_m = max(prev.bottom_m, bottom)
        if _plain(layer.description_original) not in ("", _plain(prev.description_original)):
            prev.description_original += f" / {layer.description_original}"
            if prev.description_en and layer.description_en:
                prev.description_en += f" / {layer.description_en}"
        if conflicts := _carry_properties(prev, layer):
            notes.append(f"properties differ across the sheet break: layer {prev.top_m:g}–{prev.bottom_m:g} m, "
                         + "; ".join(conflicts))
        return True
    if join == "extend":
        notes.append(f"layer {top:g}–{bottom:g} m restated on page {src.page} as continued from the previous "
                     f"sheet; top set to {prev.bottom_m:g} m")
        layer.top_m = top = prev.bottom_m
    if (prev and abs(prev.top_m - top) < 0.01 and abs(prev.bottom_m - bottom) < 0.01
            and _same_properties(prev, layer)):
        return True  # same layer read twice (overlapping sheets); a different material stays as an overlap
    if (prev and not _same_page(src, prev.source) and abs(prev.bottom_m - bottom) < 0.01
            and prev.top_m < top < prev.bottom_m and _same_properties(prev, layer)):
        # The earlier sheet gave the unit's full range, the next one repeats it from its sheet top.
        notes.append(f"layer {top:g}–{bottom:g} m on page {src.page} repeats {prev.top_m:g}–{prev.bottom_m:g} m")
        return True
    if (prev and not _same_page(src, prev.source) and abs(prev.top_m - top) < 0.01
            and bottom < prev.bottom_m - 0.01 and _same_properties(prev, layer)):
        # The earlier sheet ran its last unit past its own end; the next sheet shows where it really ends.
        notes.append(f"layer {prev.top_m:g}–{prev.bottom_m:g} m ends at {bottom:g} m per page {src.page}")
        layers[-1] = layer
        return True
    return False


SAMPLE_MAX_M = 0.6
SAMPLE_GAP_MAX_M = 1.6


def extend_sampled_layers(bh: Borehole) -> list[str]:
    """Logs that describe the soil at each sample (SPT 0.45 m, then nothing until the next sample) leave
    gaps between samples. A short described interval followed by a gap of at most SAMPLE_GAP_MAX_M to the
    next layer is extended to it, as an inferred boundary (never released without a look)."""
    notes = []
    layers = sorted(bh.layers, key=lambda layer: layer.top_m)
    for a, b in zip(layers, layers[1:]):
        gap = b.top_m - a.bottom_m
        if a.top_m > 0 and a.bottom_m - a.top_m <= SAMPLE_MAX_M and 0.1 < gap <= SAMPLE_GAP_MAX_M:  # a top layer is no sample
            notes.append(f"boundary inferred: layer {a.top_m:g}–{a.bottom_m:g} m (a sample) extended to the next "
                         f"description at {b.top_m:g} m")
            a.bottom_m = b.top_m
    return notes


CORE_LOSS_TOLERANCE_M = 0.05


def fill_core_loss(bh: Borehole) -> list[str]:
    """A gap inside one core run with less than full recovery, no longer than the unrecovered length,
    is core loss: fill it with a no_recovery layer and say so."""
    notes = []
    budget: dict[int, float] = {}  # unrecovered length left per core run: two gaps cannot spend it twice
    layers = sorted(bh.layers, key=lambda layer: layer.top_m)
    for a, b in zip(layers, layers[1:]):
        gap = b.top_m - a.bottom_m
        if gap <= CORE_LOSS_TOLERANCE_M:
            continue
        run = next((c for c in bh.core_runs if c.top_m - CORE_LOSS_TOLERANCE_M <= a.bottom_m
                    and b.top_m <= c.bottom_m + CORE_LOSS_TOLERANCE_M), None)
        tcr = run.total_core_recovery_pct if run else None
        if tcr is None or tcr >= 100:
            continue
        left = budget.setdefault(id(run), (1 - tcr / 100) * (run.bottom_m - run.top_m))
        if gap > left + CORE_LOSS_TOLERANCE_M:
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
    precise = [b for b in boreholes if is_precise(b.location)]
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


def flag_shared_coordinates(boreholes: list[Borehole]) -> None:
    """Two holes at differently named places cannot share a position to the last printed digit: a header
    copied from another sheet. A redrilled hole at the same named place may."""
    by_pair: dict[tuple[float, float], list[Borehole]] = defaultdict(list)
    for b in boreholes:
        pair = printed_pair(b.location.coords_raw)
        if pair and b.location_name:
            by_pair[pair].append(b)
    place = lambda b: re.sub(r"\W+", "", b.location_name.lower())
    for holes in by_pair.values():
        for a, b in combinations(holes, 2):
            if place(a) != place(b) and norm_name(a.borehole_name) != norm_name(b.borehole_name):
                a.issues.append(f"same printed coordinates as {b.borehole_name} at another place ({b.location_name})")
                b.issues.append(f"same printed coordinates as {a.borehole_name} at another place ({a.location_name})")


def unstack_place_positions(boreholes: list[Borehole], keep: Collection[str] = ()) -> None:
    """Several holes of one folder or upload batch at one position from the place name: the point is the
    place's centre, where none of them need be, and the map would stack distinct holes on one spot. Their
    position is left to a reviewer; a single hole keeps its approximate one. `keep`: records a reviewer
    decided or corrected."""
    stacks: dict[tuple, list[Borehole]] = defaultdict(list)
    for bh in boreholes:
        loc = bh.location
        if loc.method == GeorefMethod.geocoded_place and loc.lat is not None and loc.lon is not None:
            stacks[(_group(bh), round(loc.lat, 6), round(loc.lon, 6))].append(bh)
    for holes in stacks.values():
        if len(holes) < 2:
            continue
        for bh in holes:
            if bh.borehole_id in keep:
                continue
            bh.location = bh.location.model_copy(update={"lat": None, "lon": None, "method": None,
                                                         "accuracy_m": None, "confidence": 0.0})
            bh.issues = [i for i in bh.issues if i != "position from place name only"]
            bh.issues.append(f"not georeferenced: {len(holes)} holes of this folder or upload batch would share "
                             "one position from the place name")
            corrections.rescore(bh)


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


# Sheets of one hole repeat the same header; beyond these differences a sheet shows another hole.
POSITION_TOLERANCE_M = 25.0
ELEVATION_TOLERANCE_M = 0.5


def _distance_m(ref: XLog, log: XLog, doc_crs: tuple[str, int] | None,
                doc_place: tuple[float, float, float] | None) -> float | None:
    """Distance between the printed coordinates of two sheets: `ref` resolved as for the record, `log` by its
    reading nearest to that, so a notation that leaves axes or zone open does not create a difference.
    None if `ref` gives no position, infinity if `log` gives none."""
    crs = ref.crs_raw or log.crs_raw or (doc_crs[0] if doc_crs else None)
    country = ref.country or log.country
    loc = georeference(ref.coords_raw, crs, country, *(doc_place or (None, None, None))).location
    if loc.lat is None or loc.lon is None or loc.method != GeorefMethod.stated_coordinates:
        return None
    readings = candidates(log.coords_raw, crs, country, (loc.lat, loc.lon))
    return min((haversine_km(loc.lat, loc.lon, c.lat, c.lon) * 1000 for c in readings), default=math.inf)


def _header_difference(sheets: list[tuple[XLog, SourceRef]], log: XLog, doc_crs: tuple[str, int] | None,
                       doc_place: tuple[float, float, float] | None) -> tuple[str, int] | None:
    """How `log` prints another position or ground level than any earlier sheet of its hole, and the page of
    the earlier value."""
    worst: dict[str, tuple[float, int]] = {}
    for x, src in sheets:
        if not x.layers:
            continue
        if x.coords_raw and log.coords_raw and " ".join(x.coords_raw.split()) != " ".join(log.coords_raw.split()):
            d = _distance_m(x, log, doc_crs, doc_place)
            if d is not None and d > max(POSITION_TOLERANCE_M, worst.get("coordinates", (0.0,))[0]):
                worst["coordinates"] = (d, src.page)
        if x.elevation_m is not None and log.elevation_m is not None:
            d = abs(x.elevation_m - log.elevation_m)
            if d > max(ELEVATION_TOLERANCE_M, worst.get("ground level", (0.0,))[0]):
                worst["ground level"] = (d, src.page)
    if not worst:
        return None
    parts = ["coordinates give no position" if d == math.inf else f"{what} {d:.0f} m apart" if what == "coordinates"
             else f"{what} {d:.1f} m apart" for what, (d, _) in worst.items()]
    return " and ".join(parts), next(iter(worst.values()))[1]


def _same_id(a: str, b: str) -> bool:
    """Report or archive numbers as printed ('S.I. No. 12' = 'SI No 12'); one may add a number the other
    leaves out."""
    na = {n.lstrip("0") for n in re.findall(r"\d+", a)}
    nb = {n.lstrip("0") for n in re.findall(r"\d+", b)}
    if na and nb:
        return na <= nb or nb <= na
    return re.sub(r"\W+", "", a.lower()) == re.sub(r"\W+", "", b.lower())


def _words(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]{2,}", plain))


def _same_text(a: str, b: str) -> bool:
    """Header texts read with different spelling, case or abbreviation still share most of their words."""
    wa, wb = _words(a), _words(b)
    return not wa or not wb or 2 * len(wa & wb) >= min(len(wa), len(wb))


def _first_sheet_total(sheet: str | None) -> int | None:
    m = SHEET.search(sheet or "")
    return int(m.group(2)) if m and int(m.group(1)) == 1 else None


def _identity_difference(sheets: list[tuple[XLog, SourceRef]], log: XLog, restart: bool) -> str | None:
    """How the header of `log` shows another investigation than the earlier sheets of its hole: another
    report or project number, or at least two of project, site, client and contractor. A sheet whose depths
    restart at the surface is also told apart by another drilling date or a sheet count of its own ('1 of 1'
    after '1 of 3'); continuation sheets may carry the date they were drilled on."""
    earlier = [x for x, _ in sheets if x.layers]
    found: list[str] = []
    ids = [x.project_id for x in earlier if x.project_id]
    if log.project_id and ids and not any(_same_id(i, log.project_id) for i in ids):
        found.append(f"report/project id '{log.project_id}' vs '{ids[0]}'")
    texts = [field.replace("_", " ") for field in ("project_name", "location_text", "client", "contractor")
             if (value := getattr(log, field)) and (others := [getattr(x, field) for x in earlier if getattr(x, field)])
             and not any(_same_text(o, value) for o in others)]
    if len(texts) >= 2:
        found.append(f"{', '.join(texts)} differ")
    if restart:
        dates = {d for x in earlier if (d := _date(x.start_date))}
        if (d := _date(log.start_date)) and dates and d not in dates:
            found.append(f"drilled {d.isoformat()} vs {min(dates).isoformat()}")
        totals = {t for x in earlier if (t := _first_sheet_total(x.sheet))}
        if (t := _first_sheet_total(log.sheet)) and totals and t not in totals:
            found.append(f"sheet 1 of {t} after 1 of {min(totals)}")
    return "; ".join(found) or None


def _repeats(sheets: list[tuple[XLog, SourceRef]], log: XLog) -> int | None:
    """The page of an earlier sheet with the same layers as `log`: the same sheet copied twice into a report."""
    depth = lambda v: None if v is None else round(v, 2)
    layers = lambda x: [(depth(l.top_m), depth(l.bottom_m), l.primary) for l in x.layers]
    return next((src.page for x, src in sheets if x.layers and layers(x) == layers(log)), None)


def split_holes(logs: list[tuple[XLog, SourceRef]], doc_crs: tuple[str, int] | None = None,
                doc_place: tuple[float, float, float] | None = None
                ) -> list[tuple[list[tuple[XLog, SourceRef]], list[str], str]]:
    """Sheets grouped under one name, as (sheets, findings, naming) per hole: 'named' for the hole that bears
    the name, 'unnamed' for a split-off sheet whose name may be the previous hole's, 'qualified' for each of
    several holes that share the name.
    A sheet whose depths start again near the surface is another hole when its header shows another
    investigation (compilations of older reports each have a 'Borehole No. 2'), and then all holes of the
    name are qualified. It is also another hole when it prints another position or ground level: the model
    gives an unnamed sheet the name of the previous one. Without the restart the sheets stay together, and
    either way the difference goes to a person."""
    runs: list[tuple[list[tuple[XLog, SourceRef]], list[str], str]] = []
    loose: list[XLog] = []  # summary and table entries, matched by name only
    for log, src in logs:
        if not log.layers:
            loose.append(log)
        if not runs:
            runs.append(([(log, src)], [], "named"))
            continue
        if not log.layers:  # a summary or coordinate table entry belongs to the hole that bears the name
            runs[0][0].append((log, src))
            continue
        sheets, findings, _ = runs[-1]
        top = min((x.top_m for x in log.layers if x.top_m is not None), default=None)
        reached = max((x.bottom_m for s, _ in sheets for x in s.layers if x.bottom_m is not None), default=0.0)
        restart = top is not None and top < 0.5 and reached > top + 0.5
        identity = _identity_difference(sheets, log, restart)
        diff = _header_difference(sheets, log, doc_crs, doc_place)
        if diff is None and identity is None:
            if restart and (page := _repeats(sheets, log)) is not None:
                # Read once: the copy adds a source, not a second layer sequence.
                findings.append(f"page {src.page} repeats the layers of page {page}; read once")
                log = log.model_copy(update={"layers": [], "spt": [], "samples": [], "core_runs": [],
                                             "lab_results": []})
            sheets.append((log, src))
            continue
        what = " and ".join(filter(None, (diff[0] if diff else None, identity)))
        ref_page = diff[1] if diff else next(s.page for x, s in sheets if x.layers)
        if restart:
            findings.append(f"sheet header differs on page {src.page}: {what}; split off as a separate hole")
            name = (" of the same name" if identity else
                    f", the name '{log.name}' on this sheet may be the previous hole's")
            runs.append(([(log, src)], [f"sheet header differs from page {ref_page}: {what}, depths restart at "
                                        f"{top:g} m; recorded as a separate hole{name}"],
                         "qualified" if identity else "unnamed"))
        else:
            findings.append(f"sheet header differs on page {src.page} from page {ref_page}: {what}; "
                            f"check that the sheets belong to one hole")
            sheets.append((log, src))
    if any(naming == "qualified" for _, _, naming in runs):
        # An entry found by the name alone cannot tell which of the holes it belongs to.
        sheets, findings, _ = runs[0]
        runs[0] = ([(x, s) for x, s in sheets if not any(x is y for y in loose)], findings, "qualified")
    return runs


def build_borehole(doc_id: str, logs: list[tuple[XLog, SourceRef]],
                   coord_rows: dict[str, XCoordinateRow], doc_place: tuple[float, float, float] | None,
                   doc_crs: tuple[str, int] | None = None, kind: str | None = None) -> tuple[Borehole, int]:
    """The record (not yet scored) and the number of unreadable items that count against its release.
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
    row = lookup_hole(coord_rows, key)
    # A surveyor's coordinate table can name another system than the log template prints: both are
    # tried and the named place decides.
    table_crs = row.crs_raw if row and coords_raw and row.crs_raw and row.crs_raw != crs_raw else None
    if table_crs and not crs_raw:
        crs_raw, table_crs = table_crs, None
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
    geo = georeference(coords_raw, "; ".join(filter(None, (crs_raw, table_crs))) or None, country,
                       *(place or (None, None, None)), place_name=location_name)
    location = geo.location
    resolved = location.crs_epsg if location.crs_epsg is not None else location.crs_proj
    if table_crs and resolved in crs_from_hint(table_crs) and resolved not in crs_from_hint(crs_raw):
        geo.issues.append(f"coordinate system taken from the coordinate table: {table_crs} (the log states {crs_raw})")
        location.confidence = min(location.confidence or 0.0, 0.8)  # the printed statements contradict
    if location.method == GeorefMethod.stated_coordinates and method == GeorefMethod.linked_table:
        location.method = method
        location.is_planned = row.is_planned if row else None
    if from_document:
        location.crs_raw = None  # not printed on this log
        if resolved in crs_from_hint(doc_crs[0]):
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
        lab_results=[_lab_result(lab, v, lab.top_m, lab.bottom_m, src)
                     for x, src in logs for lab in x.lab_results
                     if (v := _lab_value(lab.parameter, lab.value_raw, lab.unit_raw)) is not None],
        sources=[src for _, src in logs],
    )
    layer_notes += fill_core_loss(bh)
    layer_notes += extend_sampled_layers(bh)
    issues = bh.validate_log() + layer_notes + geo.issues + unit_findings(xs)
    # Items that count come first, so a rescore from the kept findings counts them too.
    unreadable = sorted((u for x in xs for u in x.unreadable), key=lambda u: not counts_against(u))
    issues += [f"unreadable: {u}" for u in unreadable[:5]]
    bh.issues = list(dict.fromkeys(issues))
    return bh, sum(map(counts_against, unreadable))


def placeholder(doc_id: str, source_file: str, page: int | None, issue: str, name: str | None = None) -> Borehole:
    """A blocking record for a log the run could not turn into a hole, so its page goes to a person: the hole
    `name` that failed to assemble, else a log page that came back short. Its id and name never equal a real
    hole's, so it cannot overwrite one."""
    what = "not assembled" if name is not None else "not extracted"
    stem = f"{id_key(name)}-" if name else ""
    bh = Borehole(borehole_id=f"{doc_id}:{stem}p{page}-{what.replace(' ', '-')}",
                  borehole_name=f"{name or 'log'} {what}, {doc_id} p{page}",
                  sources=[SourceRef(file=source_file, page=page)])
    bh.issues = [issue] + bh.validate_log()
    score(bh, {}, 0)
    return bh


def _log_key(log: XLog, page_kind: str) -> HoleKey:
    return hole_key(log.name, log.investigation_type if log.investigation_type in ("borehole", "test_pit", "probe")
                    else _HOLE_KIND_OF_PAGE.get(page_kind))


def _file_order(doc_id: str) -> list:
    """'x_4' before 'x_10'."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", doc_id) if t]


SHEET_GAP_M = 1.0  # a continuation sheet starts at most this far below where the previous sheet ends


def join_image_sheets(by_doc: dict[str, list[PageResult]], group_crs: dict[str, tuple[str, int] | None]) -> None:
    """A folder of single images (one log sheet per file) spreads a hole over several documents. Sheet 'k of n'
    joins the hole whose sheet 'k-1 of n' is in an earlier file of the folder when the header agrees and the depths
    continue; it is moved into the document of the first sheet, so the hole keeps that document's id."""
    singles: dict[str, list[str]] = defaultdict(list)
    for doc_id, pages in by_doc.items():
        if "__" in doc_id and len(pages) == 1:
            singles[store.document_group(doc_id)].append(doc_id)
    for group, doc_ids in singles.items():
        open_holes: dict[HoleKey, tuple[str, list[tuple[XLog, SourceRef]], tuple[int, int]]] = {}
        for doc_id in sorted(doc_ids, key=_file_order):
            r = by_doc[doc_id][0]
            if r.extraction is None or r.kind not in LOG_KINDS:
                continue
            moved: dict[str, list[XLog]] = defaultdict(list)
            for log in r.extraction.logs:
                sheet = _sheet_number(log)
                if not log.layers or sheet is None:
                    continue
                key = _log_key(log, r.kind)
                src = SourceRef(file=r.source_file, page=r.page)
                held = open_holes.get(key)
                if held and held[0] != doc_id and sheet == (held[2][0] + 1, held[2][1]) \
                        and _continues_sheet(held[1], log) \
                        and _header_difference(held[1], log, group_crs.get(group), None) is None:
                    moved[held[0]].append(log)
                    open_holes[key] = (held[0], held[1] + [(log, src)], sheet)
                else:
                    open_holes[key] = (doc_id, [(log, src)], sheet)
            for target, logs in moved.items():
                # Only the moved logs travel; tables and other holes on the image stay with its own document.
                by_doc[target].append(replace(r, labs=None, extraction=r.extraction.model_copy(
                    update={"logs": logs, "coordinate_rows": []})))
            if moved:
                gone = {id(log) for logs in moved.values() for log in logs}
                by_doc[doc_id][0] = replace(r, extraction=r.extraction.model_copy(
                    update={"logs": [log for log in r.extraction.logs if id(log) not in gone]}))


def _continues_sheet(sheets: list[tuple[XLog, SourceRef]], log: XLog) -> bool:
    """The next sheet starts within the last unit of the earlier ones (a restated unit) or just below their end."""
    ends = [(x.top_m, x.bottom_m) for s, _ in sheets for x in s.layers if x.bottom_m is not None]
    top = min((x.top_m for x in log.layers if x.top_m is not None), default=None)
    if not ends or top is None:
        return False
    last_top, bottom = max(ends, key=lambda e: e[1])
    return (last_top if last_top is not None else bottom) - 0.01 <= top <= bottom + SHEET_GAP_M


def _document_inputs(pages: list[PageResult]) -> tuple[dict[HoleKey, XCoordinateRow],
                                                       dict[HoleKey, list[tuple[XLog, SourceRef]]],
                                                       tuple[float, float, float] | None]:
    """A document's coordinate table rows and its logs, each by hole key, and its place estimate
    (lat, lon, precision in km)."""
    coord_rows: dict[HoleKey, XCoordinateRow] = {}
    groups: dict[HoleKey, list[tuple[XLog, SourceRef]]] = defaultdict(list)
    places = []
    for r in pages:
        if r.extraction is None:
            continue
        for row in r.extraction.coordinate_rows:
            k = hole_key(row.name, None)
            old = coord_rows.get(k)
            # Actual positions over planned ones, then a table that names its coordinate system.
            if old is None or (old.is_planned and not row.is_planned) or \
                    (bool(old.is_planned) == bool(row.is_planned) and row.crs_raw and not old.crs_raw):
                coord_rows[k] = row
        for log in r.extraction.logs:
            if r.kind not in LOG_KINDS and log.layers:
                # A summary or coordinate table ('depth to rock: 40 m') is no layer sequence: it adds
                # coordinates and header values to the hole, not layers.
                log = log.model_copy(update={"layers": []})
            if log.layers or log.coords_raw or r.kind in LOG_KINDS:
                groups[_log_key(log, r.kind)].append((log, SourceRef(file=r.source_file, page=r.page)))
            if log.place_lat is not None and log.place_lon is not None:
                places.append((log.place_lat, log.place_lon, log.place_precision_km or 10.0))
    if not places:
        return coord_rows, groups, None
    # Median of the more precise estimates only, so a few country-level guesses don't drag it away.
    best = min(p[2] for p in places)
    precise = [p for p in places if p[2] <= max(3 * best, 10.0)]
    return coord_rows, groups, (statistics.median(p[0] for p in precise), statistics.median(p[1] for p in precise),
                                statistics.median(p[2] for p in precise))


def _name_split_hole(bh: Borehole, doc_id: str, naming: str, page: int, nth: int) -> None:
    """Id and name of a hole split off by `split_holes`, from its first page (and its count there), so that
    nothing stored or linked for the bare name follows it."""
    suffix = f"-{nth}" if nth > 1 else ""
    if naming == "unnamed":
        bh.borehole_id = f"{doc_id}:p{page}{suffix}"
        bh.borehole_name = f"unnamed sheet, {doc_id} p{page}{suffix}"
    else:
        bh.borehole_id = f"{doc_id}:{id_key(bh.borehole_name)}-p{page}{suffix}"
        bh.borehole_name = f"{bh.borehole_name}, p{page}{suffix}"


def assemble(results: list[PageResult], *, deduplicate: bool = True,
             failed_docs: set[str] | None = None) -> list[Borehole]:
    by_doc: dict[str, list[PageResult]] = defaultdict(list)
    for r in results:
        by_doc[r.doc_id].append(r)

    # Folders of single images (e.g. one JPG per hole) form one document for CRS and position checks.
    by_group: dict[str, list[PageResult]] = defaultdict(list)
    for r in results:
        by_group[store.document_group(r.doc_id)].append(r)
    group_crs = {g: document_crs(rs) for g, rs in by_group.items()}
    join_image_sheets(by_doc, group_crs)

    boreholes: list[Borehole] = []
    built: dict[str, list[tuple[Borehole, int, dict]]] = defaultdict(list)
    for doc_id, pages in by_doc.items():
        group = store.document_group(doc_id)
        pages.sort(key=lambda r: r.page)
        coord_rows, groups, doc_place = _document_inputs(pages)
        page_texts = {(r.source_file, r.page): r.text for r in pages}
        doc_records: list[Borehole] = []
        split_pages: Counter[int] = Counter()
        for (kind, _), named in groups.items():
            for logs, findings, naming in split_holes(named, group_crs[group], doc_place):
                if not any(log.layers for log, _ in logs):
                    continue
                try:
                    # A split-off hole's name may be the previous hole's or one several holes share: no table
                    # row by name.
                    bh, unreadable = build_borehole(doc_id, logs,
                                                    coord_rows if naming == "named" else {},
                                                    doc_place, group_crs[group], kind)
                    if naming != "named":
                        page = logs[0][1].page
                        split_pages[page] += 1
                        _name_split_hole(bh, doc_id, naming, page, split_pages[page])
                    bh.issues += findings
                    built[group].append((bh, unreadable, page_texts))
                    doc_records.append(bh)
                except Exception as exc:  # one broken record must not stop the whole run
                    if failed_docs is not None:
                        failed_docs.update(r.doc_id for r in pages)
                    print(f"  ! could not assemble {doc_id}:{logs[0][0].name}: {type(exc).__name__}: {exc}")
                    page = logs[0][1].page
                    bh = placeholder(doc_id, pages[0].source_file, page, f"log not assembled: {logs[0][0].name} on "
                                     f"page {page} ({type(exc).__name__})", logs[0][0].name)
                    built[group].append((bh, 0, page_texts))
                    doc_records.append(bh)
        _unique_ids(doc_records)
        _unique_ids_by_type(doc_records)
    for group, records in built.items():
        linked, unmatched, implausible = link_lab_results([bh for bh, _, _ in records], by_group[group])
        if unmatched or implausible:
            print(f"  {group}: {linked} lab results linked, {unmatched} without a matching hole, "
                  f"{implausible} outside plausible ranges")
        flag_position_outliers([bh for bh, _, _ in records])
        flag_shared_coordinates([bh for bh, _, _ in records])
        for bh, unreadable, page_texts in records:
            score(bh, page_texts, unreadable)
            boreholes.append(bh)
    return dedupe(boreholes) if deduplicate else boreholes


DEPTH_TOLERANCE_M = 0.05
# Findings that describe a record's layer sequence or its position travel with the layers or the position.
LAYER_FINDINGS = STRUCTURAL + ("no layers extracted", "layer ", "minor discontinuity", "core loss inferred",
                               "lithology of layer", "missing sheets", "page ", "depths printed in feet",
                               "properties differ")
POSITION_FINDINGS = corrections.GEOREF_ISSUES + ("coordinate system from reviewer knowledge",)


def document_id(bh: Borehole) -> str:
    return bh.borehole_id.split(":", 1)[0]


def _group(bh: Borehole) -> str:
    """The folder or upload batch: files put together by a person, so one hole number there is one hole."""
    return store.document_group(bh.borehole_id)


def _name_tokens(name: str) -> list[str]:
    return [t.lstrip("0") or "0" if t.isdigit() else t for t in re.findall(r"[^\W\d_]+|\d+", name.upper())]


def _short_names(boreholes: list[Borehole]) -> dict[str, str]:
    """Record id -> the hole number to match by, for a record whose name ends in the full name of a hole in
    another file of its group ('<project> B 42' and 'B 42'). The short name needs a prefix and a number, and
    only whole tokens count ('AB 42' is not 'B 42')."""
    out = {}
    for bh in boreholes:
        mine = _name_tokens(bh.borehole_name)
        for other in boreholes:
            theirs = _name_tokens(other.borehole_name)
            if (_group(other) == _group(bh) and document_id(other) != document_id(bh) and 2 <= len(theirs) < len(mine)
                    and mine[-len(theirs):] == theirs and any(t.isdigit() for t in theirs)):
                out[bh.borehole_id] = hole_key(other.borehole_name, None)[1]
                break
    return out


def _where(bh: Borehole) -> str:
    src = bh.sources[0] if bh.sources else None
    if src is None:
        return document_id(bh)
    return f"{src.file} p{src.page}" if src.page else src.file


def _evidence(a: Borehole, b: Borehole) -> tuple[list[str], list[str], list[str]]:
    """What says that two records of one name are one hole, what says they are not, and a single header text
    that differs (it may just be spelt otherwise): printed positions, the report number and the header texts.
    An approximate position only contradicts, far beyond its uncertainty."""
    agree, differ = [], []
    if _group(a) == _group(b):
        agree.append("same folder or upload batch")
    la, lb = a.location, b.location
    if la.lat is not None and lb.lat is not None:
        d = haversine_km(la.lat, la.lon, lb.lat, lb.lon) * 1000
        if is_precise(la) and is_precise(lb):
            (agree if d <= POSITION_TOLERANCE_M else differ).append(f"positions {d:.0f} m apart")
        elif d > 3 * max(la.accuracy_m or 0.0, lb.accuracy_m or 0.0, 1000.0):
            differ.append(f"positions {d / 1000:.0f} km apart")
    if a.project_id and b.project_id:
        if _same_id(a.project_id, b.project_id):
            agree.append(f"report/project id '{a.project_id}'")
        else:
            differ.append(f"report/project id '{a.project_id}' vs '{b.project_id}'")
    texts = [(f.replace("_", " "), x, y) for f in ("project_name", "location_name", "client", "contractor")
             if (x := getattr(a, f)) and (y := getattr(b, f)) and _words(x) and _words(y)]
    agree += [f for f, x, y in texts if _same_text(x, y)]
    differing = [f for f, x, y in texts if not _same_text(x, y)]
    if len(differing) >= 2:
        differ.append(f"{', '.join(differing)} differ")
    return agree, differ, [f"{f} differs" for f in differing] if len(differing) == 1 else []


def _match(a: Borehole, b: Borehole) -> tuple[str, list[str], list[str]] | None:
    """'certain' (the printed positions agree), 'probable' (one folder or upload batch, the report number or two
    header texts agree) or 'conflict' (such evidence, but also a contradiction); None when the name is all they
    share. Without agreeing positions a single differing header text is enough to keep the records apart: a
    reviewer can release or reject a record, but not split a wrong combination."""
    agree, differ, soft = _evidence(a, b)
    located = any(e.startswith("positions") for e in agree)
    if not (located or any(e.startswith(("report/project id", "same folder")) for e in agree) or len(agree) >= 2):
        return None
    if differ or (soft and not located):
        return "conflict", agree, differ + soft
    return ("certain" if located else "probable"), agree, differ


def _boundaries(bh: Borehole) -> list[float]:
    return sorted({round(v, 2) for layer in bh.layers for v in (layer.top_m, layer.bottom_m)})


def _same_layers(a: Borehole, b: Borehole) -> bool:
    x, y = _boundaries(a), _boundaries(b)
    return len(x) == len(y) and all(abs(p - q) <= DEPTH_TOLERANCE_M for p, q in zip(x, y))


def _complete(bh: Borehole) -> bool:
    return not any(i.startswith("missing sheets") for i in bh.issues)


def _location_rank(bh: Borehole) -> tuple:
    loc = bh.location
    return loc.lat is not None, is_precise(loc), not loc.is_planned, loc.confidence or 0.0


# How far header values of two documents may differ; a date has to match.
COMPARE_TOLERANCE = {"depth_m": DEPTH_TOLERANCE_M, "length_m": DEPTH_TOLERANCE_M,
                     "surface_elevation_m": ELEVATION_TOLERANCE_M, "inclination_deg": 2.0}
_FILLED = ("project_id", "project_name", "client", "contractor", "location_name", "drilling_method",
           *SOURCE_CONFLICTS)


def combine(a: Borehole, b: Borehole, how: str, agree: list[str], protected: dict[str, set],
            judged: dict[str, Borehole]) -> Borehole:
    """One record of a hole from two documents, built on the record that keeps its id: a reviewed one first, the
    one the review was made under before one reached by its uid. Otherwise the layers come from one log: the
    better-checked one when both read the same boundaries, else the more detailed one, and the other sequence
    goes to review. "Better checked" is judged on the record as it will be stored (`judged`: knowledge and
    corrections applied), and a blocking finding outweighs any score. Missing header values and the better
    position are taken from the other record; values that disagree stay with the layer record and become
    findings. Tests and lab values of both are kept once."""
    same = _same_layers(a, b)
    reviewed = lambda r: (r.borehole_id in protected.get(r.borehole_id, ()), r.borehole_id in protected)
    def checked(r: Borehole) -> tuple:
        j = judged.get(r.borehole_id, r)
        return (not any(finding_severity(i) == "blocking" for i in j.issues),
                j.review_status == ReviewStatus.auto_approved,
                min(j.confidence or 0.0, j.location.confidence or 0.0), j.confidence or 0.0)

    # The layer count only breaks ties between copies of one sequence: more layers can mean over-segmented.
    rank = (lambda r: (*reviewed(r), _complete(r), *checked(r), len(r.layers))) if same else \
        (lambda r: (*reviewed(r), _complete(r), len(r.layers), *checked(r)))
    main, other = (a, b) if rank(a) >= rank(b) else (b, a)
    issues = list(main.issues)
    checks = set(main.validate_log())  # rerun below, as a filled final depth can change them
    issues = [i for i in issues if i not in checks]
    if how == "certain":
        issues.append(f"combined with the record from {_where(other)}: same name, {', '.join(agree)}")
    else:
        issues.append(f"same hole assumed in {_where(other)}: same name, {', '.join(agree)}; "
                      f"check that both documents show this hole")
    if not same:
        end = lambda r: max((layer.bottom_m for layer in r.layers), default=0.0)
        issues.append(f"sources disagree on the layer sequence: kept {len(main.layers)} layers to {end(main):g} m "
                      f"from {_where(main)}, {_where(other)} reads {len(other.layers)} layers to {end(other):g} m")
    if _location_rank(other) > _location_rank(main):
        main.location = other.location.model_copy(deep=True)
        issues = [i for i in issues if not i.startswith(POSITION_FINDINGS)]
        issues += [i for i in other.issues if i.startswith(POSITION_FINDINGS)]
    # The other record's own checks are rerun on the combined record below.
    other_checks = set(other.validate_log())
    issues += [i for i in other.issues if not i.startswith(LAYER_FINDINGS + POSITION_FINDINGS) and i not in other_checks]

    depths_moved = False
    for field in _FILLED:
        mine, theirs = getattr(main, field), getattr(other, field)
        if theirs in (None, ""):
            continue
        if mine in (None, ""):
            setattr(main, field, theirs)
            if field == "surface_elevation_m":
                main.surface_elevation_raw, main.elevation_datum = other.surface_elevation_raw, other.elevation_datum
            source = other.field_sources.get(field) or (other.sources[0] if other.sources else None)
            if source:
                main.field_sources[field] = source
            depths_moved |= field in ("depth_m", "length_m")
        elif field in SOURCE_CONFLICTS:
            rule, what, unit = SOURCE_CONFLICTS[field]
            tolerance = COMPARE_TOLERANCE.get(field)
            if (mine != theirs) if tolerance is None else abs(mine - theirs) > tolerance:
                issues.append(f"{rule} {what}: {mine}{unit} ({_where(main)}) vs {theirs}{unit} ({_where(other)})")
    main.notes = "\n".join(dict.fromkeys(n for n in (main.notes, other.notes) if n)) or None

    def new(items: list, theirs: list, key) -> list:
        seen = {key(x) for x in items}
        return [x for x in theirs if key(x) not in seen]

    samples = new(main.samples, other.samples, lambda s: (s.name, s.top_m, s.bottom_m))
    spt = new(main.spt, other.spt, lambda s: (s.depth_m, tuple(s.blows or ()), s.n_value))
    runs = new(main.core_runs, other.core_runs, lambda c: (c.top_m, c.bottom_m))
    main.samples, main.spt, main.core_runs = main.samples + samples, main.spt + spt, main.core_runs + runs
    water = _unique_groundwater(main.groundwater + other.groundwater)
    labs = [lab for lab in other.lab_results if add_lab_result(main, lab)]
    depths_moved |= bool(samples or spt or runs or labs or len(water) > len(main.groundwater))
    main.groundwater = water
    if depths_moved:  # their depths share the other log's unchecked conversion from feet
        issues += [i for i in other.issues if i.startswith("depths printed in feet")]
    main.sources = list({(s.file, s.page): s for s in main.sources + other.sources}.values())
    main.issues = list(dict.fromkeys(issues + main.validate_log()))
    return main


def _kind(bh: Borehole) -> str | None:
    """The stated investigation type, else the one the name implies ('BH-4' is a borehole)."""
    return bh.investigation_type.value if bh.investigation_type else hole_key(bh.borehole_name, None)[0]


def dedupe(boreholes: list[Borehole], protected: dict[str, set] | None = None,
           judged: dict[str, Borehole] | None = None) -> list[Borehole]:
    """One record per hole across documents (a log, its image export, a drawing of the same hole). Records of one
    name and type from different documents are combined by `_match`, and in one folder or upload batch also a name
    that ends in another file's hole name (`_short_names`); records whose evidence points both ways stay
    apart and go to a person. Holes of one document were told apart by assembly and are never combined, nor are
    records whose own sheets disagree. `protected` maps the records a reviewer decided or corrected to what
    identifies that review (uid, correction ids): such a record keeps its id and takes in an unreviewed copy; two
    records reviewed independently of each other both stay. `judged` holds the records as they will be stored, for
    choosing the better-checked copy. Changed records are scored again."""
    protected, judged = protected or {}, judged or {}
    short = _short_names(boreholes)
    clusters: dict[str, list[dict]] = defaultdict(list)
    kept: list[dict] = []
    for bh in sorted(boreholes, key=lambda b: b.borehole_id):
        entry = {"bh": bh, "ids": {bh.borehole_id}, "docs": {document_id(bh)}, "changed": False}
        kept.append(entry)
        if any(i.startswith(("sheet header differs", "same printed coordinates as")) for i in bh.issues):
            continue
        kind, number = _kind(bh), short.get(bh.borehole_id) or hole_key(bh.borehole_name, None)[1]
        review = protected.get(bh.borehole_id)
        best = None
        for cluster in clusters[number]:
            other = cluster["bh"]
            if document_id(bh) in cluster["docs"] or (kind and _kind(other) and kind != _kind(other)):
                continue
            reviews = [protected[i] for i in cluster["ids"] if i in protected]
            if review is not None and reviews and not any(review & r for r in reviews):
                continue
            match = _match(other, bh)
            if match is None:
                continue
            how, agree, differ = match
            if how == "conflict":
                why = f"{', '.join(agree)} agree, but {', '.join(differ)}"
                bh.issues.append(f"possibly the same hole as {other.borehole_name} in {_where(other)}: {why}")
                other.issues.append(f"possibly the same hole as {bh.borehole_name} in {_where(bh)}: {why}")
                entry["changed"] = cluster["changed"] = True
            elif best is None or (how == "certain" and best[1] != "certain"):
                best = (cluster, how, agree)
        if best is None:
            clusters[number].append(entry)
            continue
        cluster, how, agree = best
        kept.pop()
        cluster["bh"] = combine(cluster["bh"], bh, how, agree, protected, judged)
        cluster["ids"].add(bh.borehole_id)
        cluster["docs"].add(document_id(bh))
        cluster["changed"] = True
    for entry in kept:
        if entry["changed"]:
            corrections.rescore(entry["bh"])
    return [entry["bh"] for entry in kept]


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
    suffix such as 'BH 16 - FHT'), else by a sample name that is a hole name or that only one hole has,
    else to the only hole of the document. Returns (linked, unmatched, implausible)."""
    by_name: dict[HoleKey, Borehole] = {}
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
        bh = lookup_hole(by_name, (kind, number))
        if bh is None and kind is None:  # same number for a borehole and a pit: the depth decides
            fits = [b for (k, n), b in by_name.items() if n == number and deep_enough(b, depth)]
            bh = fits[0] if len(fits) == 1 else None
        if bh is None:
            # 'BH10CONTAM', 'BH16FHT': a known hole followed by a word. A single letter ('BH11A') is
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
        named = [row for row in r.labs.rows if row.hole_name]
        kinds = {hole_key(row.hole_name, None)[0] for row in named} - {None}
        page_kind = next(iter(kinds)) if len(kinds) == 1 else None
        if named and not kinds:
            # Only numbers, and borehole '1' and test pit '1' both exist: the type whose holes are there and
            # deep enough for every row of the sheet, so that the shallow samples don't get ambiguous.
            fitting = [k for k in {k for k, _ in by_name} - {None}
                       if all((hole := by_name.get(hole_key(row.hole_name, k))) and deep_enough(hole, row.top_m)
                              for row in named)]
            page_kind = fitting[0] if len(fitting) == 1 else None
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
            add_lab_result(bh, _lab_result(row, value, top, bottom, SourceRef(file=r.source_file, page=r.page)))
            linked += 1
    return linked, unmatched, implausible


_LEADING_VALUE = re.compile(rf"^\s*[<>≤≥~]?\s*{_NUMBER}(?:\s*[-–~]\s*{_NUMBER})?\s*")


def _lab_name(result: LabResult) -> tuple[str, str]:
    """(name, unit) without separators. The unit is what follows the number in value_raw ('< 1e-3 g/m3');
    some sheets repeat it in the row name ('Benzene g/m3'), so it is cut off the name."""
    key = lambda s: re.sub(r"\W+", "", s.lower().replace("³", "3").replace("²", "2"))
    unit = key(_LEADING_VALUE.sub("", result.value_raw or "", count=1))
    name = key(result.parameter_raw or "")
    return (name.removesuffix(unit) if unit else name), unit


def add_lab_result(bh: Borehole, result: LabResult) -> bool:
    """The same test is often on the log and on the lab sheet (only the sheet has the sample id): it is kept
    once. Catch-all parameters (chemistry) are not converted and are told apart by printed name and unit,
    since detection limits such as '<1' repeat down a column. Returns whether the result was added."""
    same = next((x for x in bh.lab_results if x.parameter == result.parameter and _same_value(x.value, result.value)
                 and x.top_m == result.top_m and x.bottom_m == result.bottom_m and x.qualifier == result.qualifier
                 and x.value_upper == result.value_upper
                 and (not x.sample_name or not result.sample_name or x.sample_name == result.sample_name)
                 and (x.parameter != LabParameter.other or _lab_name(x) == _lab_name(result))), None)
    if same is None:
        bh.lab_results.append(result)
    elif same.sample_name is None:
        same.sample_name = result.sample_name
    return same is None
