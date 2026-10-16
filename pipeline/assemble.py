"""Turn per-page extractions into canonical `Borehole` records.

- Sheets of the same hole within a document are merged (continuation layers joined).
- Coordinates from summary tables are linked to logs by hole name.
- Units are normalised here, in code, never by the model.
- The same hole appearing in several documents (e.g. PDF and PNG export) is de-duplicated.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
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
    SourceRef,
)

from .extraction_models import PageExtraction, XCoordinateRow, XLog
from .georef import georeference

LOG_KINDS = {"borehole_log", "test_pit_log", "probe_log"}


@dataclass
class PageResult:
    doc_id: str
    source_file: str
    page: int
    kind: str
    extraction: PageExtraction | None


def norm_name(name: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", name.upper())
    s = re.sub(r"NO(?=\d)", "", s)  # 'BH No. 1' → 'BH1'
    return s or name.upper()


def _num(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.search(r"[<>]?\s*(-?\d+(?:[.,]\d+)?)", raw.replace(" ", ""))
    return float(m.group(1).replace(",", ".")) if m else None


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


def _continues(prev: Layer, nxt: Layer) -> bool:
    if abs(prev.bottom_m - nxt.top_m) > 0.05:
        return False
    a = re.sub(r"\W+", " ", prev.description_original.lower()).strip()
    b = re.sub(r"\W+", " ", nxt.description_original.lower()).strip()
    return "contin" in b or "contd" in b or b.startswith(a[:25]) or a.startswith(b[:25])


def _layers(logs: list[tuple[XLog, SourceRef]], final_depth: float | None) -> list[Layer]:
    flat = [(x, src) for log, src in logs for x in log.layers]
    # Fill missing boundaries from neighbours: top ← previous bottom, bottom ← next top / final depth.
    tops = [x.top_m for x, _ in flat]
    bottoms = [x.bottom_m for x, _ in flat]
    for i in range(len(flat)):
        if tops[i] is None:
            tops[i] = bottoms[i - 1] if i > 0 else 0.0
        if bottoms[i] is None:
            bottoms[i] = tops[i + 1] if i + 1 < len(flat) and tops[i + 1] is not None else final_depth
    layers: list[Layer] = []
    for (x, src), top, bottom in zip(flat, tops, bottoms):
        if top is None or bottom is None:
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
            source=src,
        )
        if layers and _continues(layers[-1], layer):
            layers[-1].bottom_m = layer.bottom_m  # continuation across a sheet break
            continue
        if layers and abs(layers[-1].top_m - top) < 0.01 and abs(layers[-1].bottom_m - bottom) < 0.01:
            continue  # same layer read twice (overlapping sheets)
        layers.append(layer)
    return layers


def _first(values):
    return next((v for v in values if v not in (None, "", [])), None)


def build_borehole(doc_id: str, source_file: str, logs: list[tuple[XLog, SourceRef]],
                   coord_rows: dict[str, XCoordinateRow], doc_place: tuple[float, float, float] | None) -> Borehole:
    xs = [log for log, _ in logs]
    name = xs[0].name
    key = norm_name(name)
    final_depth = _first(x.final_depth_m for x in xs)

    coords_raw = _first(x.coords_raw for x in xs)
    crs_raw = _first(x.crs_raw for x in xs)
    method = GeorefMethod.stated_coordinates
    row = coord_rows.get(key)
    if row and not coords_raw:
        coords_raw, crs_raw, method = row.coords_raw, row.crs_raw or crs_raw, GeorefMethod.linked_table
    country = _first(x.country for x in xs)
    place = next(((x.place_lat, x.place_lon, x.place_precision_km or 10.0) for x in xs
                  if x.place_lat is not None and x.place_lon is not None), None) or doc_place
    geo = georeference(coords_raw, crs_raw, country, *(place or (None, None, None)),
                       place_name=_first(x.location_text for x in xs))
    location = geo.location
    if location.method == GeorefMethod.stated_coordinates and method == GeorefMethod.linked_table:
        location.method = method
        location.is_planned = row.is_planned if row else None
    location.source = logs[0][1]

    itype = _first(x.investigation_type for x in xs)
    elevation_raw = _first(x.elevation_raw for x in xs) or (row.elevation_raw if row else None)
    bh = Borehole(
        borehole_id=f"{doc_id}:{key}",
        borehole_name=name,
        investigation_type=InvestigationType(itype) if itype in ("borehole", "test_pit") else (
            InvestigationType.other if itype else None),
        location_name=_first(x.location_text for x in xs),
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
        layers=_layers(logs, final_depth),
        core_runs=[CoreRun(top_m=c.top_m, bottom_m=c.bottom_m, total_core_recovery_pct=c.tcr_pct,
                           solid_core_recovery_pct=c.scr_pct, rqd_pct=c.rqd_pct,
                           fracture_frequency_per_m=c.fracture_frequency_per_m, source=src)
                   for x, src in logs for c in x.core_runs if c.top_m is not None and c.bottom_m is not None],
        samples=[Sample(name=s.name, top_m=s.top_m, bottom_m=s.bottom_m, type=s.type, type_raw=s.type_raw, source=src)
                 for x, src in logs for s in x.samples if s.top_m is not None],
        spt=[SPT(depth_m=s.depth_m, blows=s.blows, increment_mm=s.increment_mm, n_value=s.n_value,
                 refusal=bool(s.refusal), raw=s.raw, source=src)
             for x, src in logs for s in x.spt if s.depth_m is not None],
        groundwater=[GroundwaterObservation(depth_m=g.depth_m, kind=g.kind, observed_on=_date(g.date), dry=g.dry,
                                           raw=g.raw, source=src)
                     for x, src in logs for g in x.groundwater],
        lab_results=[LabResult(parameter=lab.parameter, parameter_raw=lab.parameter_raw, value=v,
                               value_raw=f"{lab.value_raw} {lab.unit_raw or ''}".strip(), top_m=lab.top_m,
                               bottom_m=lab.bottom_m, sample_name=lab.sample_name, source=src)
                     for x, src in logs for lab in x.lab_results
                     if (v := _lab_value(lab.parameter, lab.value_raw, lab.unit_raw)) is not None],
        sources=[src for _, src in logs],
    )
    issues = bh.validate_log() + geo.issues
    unreadable = [u for x in xs for u in x.unreadable]
    issues += [f"unreadable: {u}" for u in unreadable[:5]]
    bh.issues = list(dict.fromkeys(issues))
    structural = [i for i in bh.issues if not i.startswith("unreadable") and "georeferenced" not in i
                  and "place" not in i and "CRS" not in i and "coordinates" not in i]
    bh.confidence = round(max(0.1, 0.95 - 0.1 * len(structural) - 0.05 * min(len(unreadable), 4)), 2)
    if not bh.layers:
        bh.confidence = 0.1
    return bh


def assemble(results: list[PageResult]) -> list[Borehole]:
    by_doc: dict[str, list[PageResult]] = defaultdict(list)
    for r in results:
        by_doc[r.doc_id].append(r)

    boreholes: list[Borehole] = []
    for doc_id, pages in by_doc.items():
        pages.sort(key=lambda r: r.page)
        coord_rows: dict[str, XCoordinateRow] = {}
        groups: dict[str, list[tuple[XLog, SourceRef]]] = defaultdict(list)
        places = []
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
        for logs in groups.values():
            if any(log.layers for log, _ in logs):
                boreholes.append(build_borehole(doc_id, pages[0].source_file, logs, coord_rows, doc_place))
    return dedupe(boreholes)


def dedupe(boreholes: list[Borehole]) -> list[Borehole]:
    """Same hole in several documents (e.g. a PDF and its PNG export): keep the richer record."""
    kept: dict[tuple, Borehole] = {}
    for bh in boreholes:
        loc = bh.location
        pos = (round(loc.lat, 3), round(loc.lon, 3)) if loc.lat is not None else (bh.borehole_id,)
        key = (norm_name(bh.borehole_name), pos)
        other = kept.get(key)
        if other is None:
            kept[key] = bh
            continue
        richer, poorer = (bh, other) if len(bh.layers) > len(other.layers) else (other, bh)
        richer.sources = list({(s.file, s.page): s for s in richer.sources + poorer.sources}.values())
        kept[key] = richer
    return list(kept.values())
