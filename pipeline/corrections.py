"""Reviewer corrections. They are stored apart from the extraction and re-applied after every run,
so a human fix survives re-extraction. A correction that no longer fits a new extraction is not
dropped silently: it becomes a blocking finding and the record returns to the review queue.

Kinds (payloads):
- crs:      {"epsg": 28355}                        – read the printed coordinates in this CRS
- position: {"lat": .., "lon": .., "accuracy_m": ..} – set the position by hand
- layer:    {"orig_top": .., "orig_bottom": .., "top": .., "bottom": .., "primary": .., "delete": false}
"""

from __future__ import annotations

import json

from schema.borehole import Borehole, GeorefMethod, Lithology

from . import config
from .georef import _apply, resolve_with
from .scoring import score

# Findings about the position that a reviewer's CRS or position correction settles.
GEOREF_ISSUES = (
    "CRS ambiguous", "coordinate system inferred", "coordinate axes inferred", "coordinate notation or datum ambiguous",
    "coordinate system taken from", "coordinates ", "printed coordinates", "position ", "no place to cross-check",
    "not georeferenced", "invalid WGS84",
)


def describe(kind: str, payload: dict) -> str:
    if kind == "crs":
        return f"coordinate system EPSG:{payload['epsg']}"
    if kind == "position":
        return f"position {payload['lat']:.6f}, {payload['lon']:.6f}"
    span = f"layer {payload['orig_top']:g}–{payload['orig_bottom']:g} m"
    if payload.get("delete"):
        return f"{span} deleted"
    changes = [f"{payload['top']:g}–{payload['bottom']:g} m" if "top" in payload else None,
               payload.get("primary")]
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
    return True


def apply(bh: Borehole, items: list[dict]) -> list[bool]:
    """Apply corrections in the order they were made, then re-check and re-score the record.
    Returns for each correction whether it fitted."""
    if not items:
        return []
    results = []
    before = bh.validate_log()
    notes, georef_fixed = [], False
    for c in items:
        kind, p = c["kind"], c["payload"]
        if kind == "crs":
            ok = _crs(bh, int(p["epsg"]))
        elif kind == "position":
            ok = _position(bh, float(p["lat"]), float(p["lon"]), p.get("accuracy_m"))
        else:
            ok = _layer(bh, p)
        results.append(ok)
        georef_fixed |= ok and kind in ("crs", "position")
        who = c.get("reviewer") or "a reviewer"
        notes.append(f"corrected by {who}: {describe(kind, p)}" if ok
                     else f"stale correction by {who}: {describe(kind, p)} no longer fits the extraction")
    kept = [i for i in bh.issues if i not in before and not i.startswith(("corrected by", "stale correction"))]
    if georef_fixed:
        kept = [i for i in kept if not i.startswith(GEOREF_ISSUES)]
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
    return {(file, p["page"]): p["text"] for p in json.loads(manifest.read_text())}


def rescore(bh: Borehole) -> None:
    unreadable = sum(1 for i in bh.issues if i.startswith("unreadable:"))
    score(bh, page_texts(bh), unreadable)
