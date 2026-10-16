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

from .extraction_models import PageExtraction, XCoordinateRow, XLog
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
_LAB_FACTORS: list[tuple[set[LabParameter], str, float]] = [
    ({LabParameter.ucs, LabParameter.tensile_strength, LabParameter.point_load_index, LabParameter.e_modulus}, r"^kpa$", 0.001),
    ({LabParameter.ucs, LabParameter.tensile_strength, LabParameter.point_load_index, LabParameter.e_modulus}, r"^kg/?cm2|kgf/cm", 0.0980665),
    ({LabParameter.ucs, LabParameter.tensile_strength, LabParameter.e_modulus}, r"^gpa$", 1000.0),
    ({LabParameter.cohesion, LabParameter.undrained_shear_strength}, r"^kg/?cm2|kgf/cm", 98.0665),
    ({LabParameter.cohesion, LabParameter.undrained_shear_strength}, r"^mpa$", 1000.0),
    ({LabParameter.unit_weight, LabParameter.dry_unit_weight}, r"^t/?m3|tn/?m3|g/?cm3|mg/m3", 9.80665),
]


def _lab_value(parameter: LabParameter, value_raw: str, unit_raw: str | None) -> float | None:
    value = _num(value_raw)
    if value is None:
        return None
    unit = (unit_raw or "").lower().replace(" ", "").replace("³", "3").replace("²", "2")
    for params, pattern, factor in _LAB_FACTORS:
        if parameter in params and re.search(pattern, unit):
            return value * factor
    return value


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


def _layers(logs: list[tuple[XLog, SourceRef]], final_depth: float | None,
            notes: list[str] | None = None) -> list[Layer]:
    flat = [(x, src) for log, src in logs for x in log.layers]
    # Fill missing boundaries from neighbours: top ← previous bottom, bottom ← next top / final depth.
    tops = [x.top_m for x, _ in flat]
    bottoms = [x.bottom_m for x, _ in flat]
    for i in range(len(flat)):
        if tops[i] is None:
            tops[i] = bottoms[i - 1] if i > 0 else 0.0
        if bottoms[i] is None:
            if i + 1 < len(flat):
                bottoms[i] = tops[i + 1]  # stays None if the next top is unknown too
            else:
                bottoms[i] = final_depth  # only the last layer may close at the final depth
    layers: list[Layer] = []
    for (x, src), top, bottom in zip(flat, tops, bottoms):
        if top is None or bottom is None:
            continue
        if (layers and not _same_page(src, layers[-1].source) and top < layers[-1].bottom_m < bottom
                and any(abs(l.top_m - top) < 0.05 for l in layers)):
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
        if layers and abs(layers[-1].top_m - top) < 0.01 and abs(layers[-1].bottom_m - bottom) < 0.01:
            continue  # same layer read twice (overlapping sheets)
        layers.append(layer)
    return layers


# Explicit coordinate system statements in free text (report prose, notes, legends). Weak hints such as a
# bare country name are left out on purpose: they would turn a guess into a "stated" system.
CRS_STATEMENT = re.compile(
    r"(epsg\s*:?\s*\d{4,5}|mga\s?(?:94|2020)[^\n]{0,24}|zone\s?\d{1,2}\s?[a-z]?\s?mga\s?(?:94|2020)|"
    r"utm[^\n]{0,16}?\d{1,2}\s?[a-z]?\b|osgb\s?36|british national grid|mt\.?\s?eden[^\n]{0,10}|nztm|"
    r"etrs\s?89[^\n]{0,20}|lv95|ch1903\+?|rd\s?new|lambert.?93|kgd\s?2002[^\n]{0,20}|(?:east|central|west)\s?belt|"
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
    top = Counter(epsgs for epsgs, _, _ in resolved).most_common(1)[0][0]
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
                   doc_crs: tuple[str, int] | None = None) -> tuple[Borehole, int]:
    """The record (not yet scored) and the number of items the model reported as unreadable."""
    xs = [log for log, _ in logs]
    name = xs[0].name
    key = norm_name(name)
    final_depth = _first(x.final_depth_m for x in xs)
    layer_notes: list[str] = []

    coords_raw = _first(x.coords_raw for x in xs)
    crs_raw = _first(x.crs_raw for x in xs)
    method = GeorefMethod.stated_coordinates
    row = coord_rows.get(key)
    if row and not coords_raw:
        coords_raw, crs_raw, method = row.coords_raw, row.crs_raw or crs_raw, GeorefMethod.linked_table
    from_document = bool(coords_raw and not crs_raw and doc_crs)
    if from_document:
        crs_raw = doc_crs[0]
    country = _first(x.country for x in xs)
    location_name = _first(clean_location_text(x.location_text) for x in xs)
    place = next(((x.place_lat, x.place_lon, x.place_precision_km or 10.0) for x in xs
                  if x.place_lat is not None and x.place_lon is not None), None) or doc_place
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

    itype = _first(x.investigation_type for x in xs)
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
                               bottom_m=lab.bottom_m, sample_name=lab.sample_name, source=src)
                     for x, src in logs for lab in x.lab_results
                     if (v := _lab_value(lab.parameter, lab.value_raw, lab.unit_raw)) is not None],
        sources=[src for _, src in logs],
    )
    issues = bh.validate_log() + layer_notes + geo.issues
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
                k = norm_name(row.name)
                if k not in coord_rows or (coord_rows[k].is_planned and not row.is_planned):
                    coord_rows[k] = row
            for log in r.extraction.logs:
                if log.layers or log.coords_raw or r.kind in LOG_KINDS:
                    groups[norm_name(log.name)].append((log, SourceRef(file=r.source_file, page=r.page)))
                if log.place_lat is not None and log.place_lon is not None:
                    places.append((log.place_lat, log.place_lon, log.place_precision_km or 10.0))
        doc_place = None
        if places:
            doc_place = (statistics.median(p[0] for p in places), statistics.median(p[1] for p in places),
                         statistics.median(p[2] for p in places))
        doc_records: list[Borehole] = []
        for logs in groups.values():
            if any(log.layers for log, _ in logs):
                try:
                    bh, unreadable = build_borehole(doc_id, pages[0].source_file, logs, coord_rows, doc_place,
                                                    group_crs[group])
                    built[group].append((bh, unreadable, page_texts))
                    doc_records.append(bh)
                except Exception as exc:  # one broken record must not stop the whole run
                    print(f"  ! could not assemble {doc_id}:{logs[0][0].name}: {type(exc).__name__}: {exc}")
        _unique_ids(doc_records)
    for records in built.values():
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
        richer, poorer = (bh, other) if len(bh.layers) > len(other.layers) else (other, bh)
        richer.sources = list({(s.file, s.page): s for s in richer.sources + poorer.sources}.values())
        kept[key] = richer
    return list(kept.values())
