"""Reviewer corrections. They are stored apart from the extraction and re-applied after every run,
so a human fix survives re-extraction. A correction that no longer fits a new extraction is not
dropped silently: it becomes a blocking finding and the record returns to the review queue.

Kinds (payloads):
- crs:      {"epsg": 28355}                        – read the printed coordinates in this CRS
- position: {"lat": .., "lon": .., "accuracy_m": ..} – set the position by hand
- layer:    {"orig_top": .., "orig_bottom": .., "top": .., "bottom": .., "primary": .., "delete": false,
             "description_original": .., "stratigraphic_code": .., "stratigraphic_name": .., "geologic_age": ..}
- layer_add: {"top": .., "bottom": .., "primary": .., "description_original": ..} – a layer the extraction missed
- field:    {"field": "surface_elevation_m", "value": 35.7}   – a header value (see FIELDS)
- groundwater: {"depth_m": 4.5} or {"dry": true}              – a groundwater observation
- test:     {"test": "spt"|"core"|"lab", "orig_depth": .., "parameter": .. (lab), "value": .., "rqd": .. (core),
             "delete": false} – SPT N, core TCR/RQD or a lab value
"""

from __future__ import annotations

import json
import re

from datetime import date

from schema.borehole import Borehole, GeorefMethod, GroundwaterObservation, Layer, Lithology

from . import config
from .georef import _apply, resolve_with
from .scoring import SOURCE_CONFLICTS, counts_against, score

# Findings about the position that a reviewer's CRS or position correction settles.
GEOREF_ISSUES = (
    "CRS ambiguous", "coordinate system inferred", "coordinate axes inferred", "coordinate notation or datum ambiguous",
    "coordinate system taken from", "coordinates ", "printed coordinates", "position ", "no place to cross-check",
    "not georeferenced", "invalid WGS84", "coordinate system read differently",
)
# Items the model could not read about the coordinates (not about levels), settled by the same fix.
GEOREF_UNREADABLE = re.compile(r"^unreadable:(?!.*\b(?:elevation|level)\b).*\b(?:coordinat\w*|crs|datum|grid|zone|easting|northing)\b", re.I)


# Header fields a reviewer may set, with their type.
FIELDS: dict[str, type] = {
    "borehole_name": str, "project_name": str, "location_name": str, "surface_elevation_m": float,
    "elevation_datum": str, "depth_m": float, "start_date": date, "end_date": date, "notes": str,
}
LAYER_TEXT = ("description_original", "stratigraphic_code", "stratigraphic_name", "geologic_age")


def describe(kind: str, payload: dict) -> str:
    if kind == "field":
        return f"{payload['field'].replace('_', ' ')} = {payload['value']}"
    if kind == "layer_add":
        return f"layer {payload['top']:g}–{payload['bottom']:g} m added ({payload['primary']})"
    if kind == "groundwater":
        return "groundwater: dry" if payload.get("dry") else f"groundwater at {payload['depth_m']:g} m"
    if kind == "crs":
        return f"coordinate system EPSG:{payload['epsg']}"
    if kind == "position":
        return f"position {payload['lat']:.6f}, {payload['lon']:.6f}"
    if kind == "test":
        what = f"{payload.get('parameter') or payload['test']} at {payload['orig_depth']:g} m"
        return f"{what} deleted" if payload.get("delete") else f"{what} → {payload['value']:g}"
    span = f"layer {payload['orig_top']:g}–{payload['orig_bottom']:g} m"
    if payload.get("delete"):
        return f"{span} deleted"
    changes = [f"{payload['top']:g}–{payload['bottom']:g} m" if "top" in payload else None,
               payload.get("primary"), *(f"{k.replace('_', ' ')}" for k in LAYER_TEXT if k in payload)]
    return f"{span} → " + ", ".join(c for c in changes if c)


def _crs(bh: Borehole, epsg: int) -> bool:
    loc = bh.location
    if not loc.coords_raw:
        return False
    near = (loc.lat, loc.lon) if loc.lat is not None and loc.lon is not None else None
    c = resolve_with(loc.coords_raw, epsg, near)
    if c is None:
        return False
    method = loc.method if loc.method == GeorefMethod.linked_table else GeorefMethod.stated_coordinates
    _apply(loc, c, method)
    loc.crs_candidates, loc.confidence, loc.accuracy_m = [], 1.0, 10.0
    return True


def _position(bh: Borehole, lat: float, lon: float, accuracy_m: float | None) -> bool:
    loc = bh.location
    loc.lat, loc.lon, loc.method = lat, lon, GeorefMethod.manual
    loc.crs_candidates, loc.confidence, loc.accuracy_m = [], 1.0, accuracy_m
    return True


def _layer(bh: Borehole, p: dict) -> bool:
    """The layer is found by the depths the reviewer saw. Corrections are applied in order, so a later
    one may refer to depths set by an earlier one."""
    i = next((i for i, l in enumerate(bh.layers)
              if abs(l.top_m - p["orig_top"]) < 0.01 and abs(l.bottom_m - p["orig_bottom"]) < 0.01), None)
    if i is None:
        return False  # the extraction changed
    if p.get("delete"):
        del bh.layers[i]
        return True
    layer = bh.layers[i]
    if "top" in p:
        layer.top_m, layer.bottom_m = float(p["top"]), float(p["bottom"])
    if "primary" in p:
        layer.primary = Lithology(p["primary"])
    for key in LAYER_TEXT:
        if key in p:
            setattr(layer, key, p[key] or (None if key != "description_original" else layer.description_original))
    return True


def _layer_add(bh: Borehole, p: dict) -> bool:
    top, bottom = float(p["top"]), float(p["bottom"])
    same = next((l for l in bh.layers if abs(l.top_m - top) < 0.01 and abs(l.bottom_m - bottom) < 0.01), None)
    if same is not None:  # the extraction has the interval now: the reviewer's reading still wins
        same.primary = Lithology(p["primary"])
        if p.get("description_original"):
            same.description_original = p["description_original"]
        return True
    bh.layers.append(Layer(top_m=top, bottom_m=bottom, primary=Lithology(p["primary"]),
                           description_original=p.get("description_original") or "added by reviewer"))
    bh.layers.sort(key=lambda layer: layer.top_m)
    return True


def _test(bh: Borehole, p: dict) -> bool:
    """The result is found by its kind and the depth the reviewer saw; a lab result also by its parameter."""
    rows = {"spt": bh.spt, "core": bh.core_runs, "lab": bh.lab_results}[p["test"]]
    depth = {"spt": lambda r: r.depth_m, "core": lambda r: r.top_m, "lab": lambda r: r.top_m or 0}[p["test"]]
    i = next((i for i, r in enumerate(rows) if abs(depth(r) - p["orig_depth"]) < 0.01
              and (p["test"] != "lab" or r.parameter.value == p.get("parameter"))), None)
    if i is None:
        return False
    if p.get("delete"):
        del rows[i]
        return True
    row = rows[i]
    if p["test"] == "spt":
        row.n_value, row.refusal = int(p["value"]), False
    elif p["test"] == "core":
        row.total_core_recovery_pct = p["value"]
        if p.get("rqd") is not None:
            row.rqd_pct = p["rqd"]
    else:
        row.value, row.qualifier = p["value"], None
        row.value_upper = None
    return True


def _field(bh: Borehole, p: dict) -> bool:
    kind = FIELDS.get(p["field"])
    if kind is None:
        return False
    value = p["value"]
    if value is not None and kind is date:
        value = date.fromisoformat(str(value))
    elif value is not None:
        value = kind(value)
    setattr(bh, p["field"], value)
    return True


def _groundwater(bh: Borehole, p: dict) -> bool:
    depth = p.get("depth_m")
    if not any(g.depth_m == depth and bool(g.dry) == bool(p.get("dry")) for g in bh.groundwater):
        bh.groundwater.append(GroundwaterObservation(depth_m=depth, dry=p.get("dry"), raw="added by reviewer"))
    return True


def _overlapping_fixes(bh: Borehole, items: list[dict]) -> set[int]:
    """Layer fixes whose layer ends up overlapping another one: made while the extraction lacked what lies there
    now (e.g. a sheet joined later), they would double it. Judged on the end result, since a reviewer's edits pass
    through overlaps (extend one layer, then delete the next)."""
    trial = bh.model_copy(deep=True)
    owner: dict[int, int] = {}  # layer → the fix that last set its range
    for n, c in enumerate(items):
        if c["kind"] in ("layer", "layer_add"):
            before = {id(l): (l.top_m, l.bottom_m) for l in trial.layers}
            (_layer_add if c["kind"] == "layer_add" else _layer)(trial, c["payload"])
            owner.update({id(l): n for l in trial.layers if before.get(id(l)) != (l.top_m, l.bottom_m)})
    return {owner[id(a)] for a in trial.layers if id(a) in owner
            and any(b is not a and min(a.bottom_m, b.bottom_m) - max(a.top_m, b.top_m) > 0.01 for b in trial.layers)}


def apply(bh: Borehole, items: list[dict]) -> list[bool]:
    """Apply corrections in the order they were made, then re-check and re-score the record.
    Returns for each correction whether it fitted."""
    if not items:
        return []
    results = []
    existing = list(bh.issues)
    before = bh.validate_log()
    notes, georef_fixed, fields_fixed = [], False, set()
    overlapping = _overlapping_fixes(bh, items)
    for n, c in enumerate(items):
        kind, p = c["kind"], c["payload"]
        if n in overlapping:
            ok = False
        elif kind == "crs":
            ok = _crs(bh, int(p["epsg"]))
        elif kind == "position":
            ok = _position(bh, float(p["lat"]), float(p["lon"]), p.get("accuracy_m"))
        elif kind == "layer_add":
            ok = _layer_add(bh, p)
        elif kind == "field":
            ok = _field(bh, p)
        elif kind == "groundwater":
            ok = _groundwater(bh, p)
        elif kind == "test":
            ok = _test(bh, p)
        else:
            ok = _layer(bh, p)
        results.append(ok)
        georef_fixed |= ok and kind in ("crs", "position")
        if ok and kind == "field":
            fields_fixed.add(p["field"])
        who = c.get("reviewer") or "a reviewer"
        notes.append(f"corrected by {who}: {describe(kind, p)}" if ok
                     else f"stale correction by {who}: {describe(kind, p)} no longer fits the extraction")
    kept = [i for i in existing if i not in before]
    if georef_fixed:
        kept = [i for i in kept if not i.startswith(GEOREF_ISSUES) and not GEOREF_UNREADABLE.search(i)]
    # A reviewer's value settles a disagreement between the documents of the hole.
    settled = tuple(f"{rule} {what}:" for field, (rule, what, _) in SOURCE_CONFLICTS.items() if field in fields_fixed)
    if settled:
        kept = [i for i in kept if not i.startswith(settled)]
    bh.issues = list(dict.fromkeys(bh.validate_log() + kept + notes))
    rescore(bh)
    return results


def page_texts(bh: Borehole) -> dict[tuple[str, int | None], str]:
    """Text layers of the record's pages, from the render manifest (for the text agreement check)."""
    if not bh.sources:
        return {}
    manifest = config.PAGES_DIR / bh.borehole_id.split(":", 1)[0] / "manifest.json"
    if not manifest.exists():
        return {}
    file = bh.sources[0].file
    return {(file, p["page"]): p["text"] for p in json.loads(manifest.read_text(encoding="utf-8"))}


def rescore(bh: Borehole) -> None:
    unreadable = sum(1 for i in bh.issues if i.startswith("unreadable:") and counts_against(i))
    score(bh, page_texts(bh), unreadable)
