"""Boreholes with similar ground: the lithology mix per depth band, the rock head and the water level.

Lithology classes are grouped coarsely so that 'fine sand' and 'medium sand' count as the same ground, and holes
of the same document are left out: the question is which other project met comparable conditions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from pipeline.ground import NO_DATA, SOIL
from schema.borehole import Lithology

L = Lithology
BANDS = ((0.0, 5.0), (5.0, 10.0), (10.0, 20.0), (20.0, 40.0))  # metres below ground
GROUP_OF = {
    **{c: "made ground" for c in (L.topsoil, L.fill)},
    **{c: "gravel" for c in (L.gravel, L.coarse_gravel, L.medium_gravel, L.fine_gravel, L.cobbles, L.boulders,
                             L.scree)},
    **{c: "sand" for c in (L.sand, L.coarse_sand, L.medium_sand, L.fine_sand, L.volcanic_ash)},
    **{c: "silt" for c in (L.silt, L.loess, L.loess_loam, L.slope_loam, L.meadow_lime)},
    **{c: "clay" for c in (L.clay, L.marsh_clay, L.varved_clay, L.boulder_clay, L.till_marl)},
    **{c: "organic" for c in (L.peat_humus, L.gyttja, L.lignite)},
}
GROUPS = ("made ground", "gravel", "sand", "silt", "clay", "organic", "rock")
MIN_BAND_COVER = 0.5  # share of a band the log must describe for the band to count
UNKNOWN_BAND = 0.5
WATER_WEIGHT = 0.15
WATER_SCALE_M = 5.0  # water levels this far apart count as fully different


def group(code: str) -> str | None:
    lith = Lithology(code)
    if lith in NO_DATA:
        return None
    return GROUP_OF.get(lith) or ("rock" if lith not in SOIL else None)


@dataclass
class Profile:
    bands: list[dict[str, float] | None] = field(default_factory=list)  # group shares per band; None: not logged
    rock_head_m: float | None = None
    rock_top_m: float | None = None  # shallowest rock layer, also above soil or weathered to soil (no rock head)
    groundwater_m: float | None = None


def profile(layers: list[tuple[float | None, float | None, str]], ground: dict | None = None) -> Profile:
    bands: list[dict[str, float] | None] = []
    for top, bottom in BANDS:
        metres: dict[str, float] = {}
        for t, b, code in layers:
            g = group(code) if t is not None and b is not None else None
            overlap = min(b, bottom) - max(t, top) if g else 0.0
            if overlap > 0:
                metres[g] = metres.get(g, 0.0) + overlap
        logged = sum(metres.values())
        bands.append({g: m / logged for g, m in metres.items()} if logged >= MIN_BAND_COVER * (bottom - top) else None)
    rock_tops = [t for t, b, code in layers if t is not None and group(code) == "rock"]
    ground = ground or {}
    return Profile(bands, ground.get("rock_head_m"), min(rock_tops, default=None), ground.get("groundwater_m"))


def distance(a: Profile, b: Profile) -> float | None:
    """How far `b` is from the hole `a`: 0 for the same ground, 1 for nothing in common; None when no depth band
    is logged in both. A band `a` logs and `b` does not counts as half different, so a shallow hole does not
    match a deep one on its top metres alone."""
    logged = [(x, y) for x, y in zip(a.bands, b.bands) if x is not None]
    if not any(y is not None for _, y in logged):
        return None
    lith = sum(UNKNOWN_BAND if y is None else sum(abs(x.get(g, 0.0) - y.get(g, 0.0)) for g in GROUPS) / 2
               for x, y in logged) / len(logged)
    if a.groundwater_m is None or b.groundwater_m is None:
        return lith
    water = min(abs(a.groundwater_m - b.groundwater_m) / WATER_SCALE_M, 1.0)
    return (1 - WATER_WEIGHT) * lith + WATER_WEIGHT * water


def reason(p: Profile) -> str:
    """Short description of a hole's ground, e.g. 'rock from 8 m, mostly clay above; water at 2 m'."""
    soil: dict[str, float] = {}
    for band in p.bands:
        for g, share in (band or {}).items():
            if g != "rock":
                soil[g] = soil.get(g, 0.0) + share
    total = sum(soil.values())
    ranked = sorted(soil, key=soil.get, reverse=True)
    if not ranked:
        main = None
    elif soil[ranked[0]] / total >= 0.6 or len(ranked) == 1:
        main = f"mostly {ranked[0]}"
    else:
        main = " and ".join(g for g in ranked[:2] if soil[g] / total >= 0.25) or f"mixed, mostly {ranked[0]}"
    if p.rock_head_m is not None:
        text = f"rock from {p.rock_head_m:g} m" + (f", {main} above" if main and p.rock_head_m > 0 else "")
    elif p.rock_top_m is not None:  # weathered to soil, or soil below it: no rock head (pipeline/ground.py)
        text = f"rock layers from {p.rock_top_m:g} m" + (f", {main} above" if main and p.rock_top_m > 0 else "")
    else:
        text = f"{main}, no rock reached" if main else "no layers to compare"
    if p.groundwater_m is not None:
        text += f"; water at {p.groundwater_m:g} m"
    return text


def similar(con: sqlite3.Connection, borehole_id: str, limit: int = 5) -> dict | None:
    """This hole's ground and the `limit` closest holes of other documents, rejected records left out; None if
    the hole is unknown."""
    rows = {r["rid"]: r for r in con.execute(
        "SELECT rid, id, name, doc_id, project, country, review_status, score, ground FROM boreholes "
        "WHERE review_status != 'rejected' OR id = ?", (borehole_id,))}
    target = next((r for r in rows.values() if r["id"] == borehole_id), None)
    if target is None:
        return None
    layers: dict[int, list[tuple[float | None, float | None, str]]] = {}
    for rid, top, bottom, code in con.execute("SELECT rid, top_m, bottom_m, primary_code FROM layers"):
        if rid in rows:
            layers.setdefault(rid, []).append((top, bottom, code))
    profiles = {rid: profile(layers.get(rid, []), json.loads(r["ground"]) if r["ground"] else None)
                for rid, r in rows.items()}
    mine = profiles[target["rid"]]
    found = []
    for rid, r in rows.items():
        if r["doc_id"] == target["doc_id"]:
            continue
        d = distance(mine, profiles[rid])
        if d is not None:
            found.append((d, r, profiles[rid]))
    found.sort(key=lambda x: (x[0], x[1]["name"] or ""))
    return {"ground": reason(mine) if any(mine.bands) else None,
            "like": like(mine, layers.get(target["rid"], [])) if any(mine.bands) else None,
            "similar": [{"id": r["id"], "name": r["name"], "project": r["project"], "country": r["country"],
                         "review_status": r["review_status"], "score": r["score"], "similarity": round(1 - d, 2),
                         "reason": reason(p)} for d, r, p in found[:limit]]}


def codes(group_name: str) -> list[str]:
    return [c.value for c in Lithology if group(c.value) == group_name]


def material_conditions(values: list[str]) -> list[tuple[str, float, float, float]]:
    """'group:from:to:metres' -> (group, from, to, metres): at least `metres` of the group between from and to."""
    out = []
    for value in values:
        parts = value.split(":")
        try:
            name, top, bottom, metres = parts[0], *(float(v) for v in parts[1:])
        except ValueError:
            raise ValueError(f"material: expected group:from_m:to_m:metres, got {value!r}") from None
        if name not in GROUPS or not 0 <= top < bottom or metres <= 0:
            raise ValueError(f"material: unknown group or empty range in {value!r}; groups: {', '.join(GROUPS)}")
        out.append((name, top, bottom, metres))
    return out


def ground_filters(rock_head_max: float | None, groundwater_max: float | None,
                   materials: list[tuple[str, float, float, float]]) -> tuple[list[str], list]:
    """WHERE clauses for the ground search. A hole without a rock head, or without a water level, does not
    match a bound on it; a material counts only where the log describes it, so a shallow hole cannot reach
    metres below its end."""
    where, params = [], []
    if rock_head_max is not None:
        where.append("json_extract(ground, '$.rock_head_m') <= ?")
        params.append(rock_head_max)
    if groundwater_max is not None:
        where.append("json_extract(ground, '$.groundwater_m') <= ?")
        params.append(groundwater_max)
    for name, top, bottom, metres in materials:
        members = codes(name)
        where.append(f"rid IN (SELECT rid FROM layers WHERE primary_code IN ({','.join('?' * len(members))}) "
                     "AND top_m < ? AND bottom_m > ? GROUP BY rid HAVING SUM(MIN(bottom_m, ?) - MAX(top_m, ?)) >= ?)")
        params += [*members, bottom, top, bottom, top, metres - 1e-9]
    return where, params


def like(p: Profile, layers: list[tuple[float | None, float | None, str]]) -> dict:
    """Ground filters that find holes like this one: its rock head and water level with some room, and its main
    soil above the rock (or in the top 20 m) as a material condition."""
    out: dict = {"rock_head_max": None, "groundwater_max": None, "material": []}
    if p.rock_head_m is not None:
        out["rock_head_max"] = round(p.rock_head_m + max(2.0, 0.25 * p.rock_head_m))
    if p.groundwater_m is not None:
        out["groundwater_max"] = round(p.groundwater_m + 1.0, 1)
    window = min(p.rock_head_m, 20.0) if p.rock_head_m is not None else 20.0
    metres: dict[str, float] = {}
    for t, b, code in layers:
        g = group(code) if t is not None and b is not None else None
        if g and g != "rock" and (overlap := min(b, window) - max(t, 0.0)) > 0:
            metres[g] = metres.get(g, 0.0) + overlap
    if metres and window > 0:
        main = max(metres, key=metres.get)
        if metres[main] >= 1.0:
            out["material"].append(f"{main}:0:{window:g}:{max(1, round(metres[main] * 0.6, 1)):g}")
    return out
