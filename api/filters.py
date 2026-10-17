"""WHERE clauses built from query parameters, for the list, the map, the statistics and the review queue."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Annotated, Literal, NamedTuple

from fastapi import Depends, HTTPException, Query

from . import similar
from .db import STATUS_GROUPS

# Every search word and every list condition deepens the WHERE expression; SQLite refuses a tree deeper than 1000.
MAX_SEARCH_WORDS = 32
MAX_CONDITIONS = 50

StatusFilter = Literal["all", "released", "pending", "rejected"]


class Where(NamedTuple):
    clauses: list[str]
    params: list

    def also(self, clause: str, params: Sequence = ()) -> Where:
        return Where([*self.clauses, clause], [*self.params, *params])

    def sql(self) -> str:
        return " AND ".join(self.clauses) or "1"


def release_filters(status: StatusFilter, score_min: float | None, score_max: float | None) -> Where:
    where = Where([], [])
    if status != "all":
        values = STATUS_GROUPS[status]
        where = where.also(f"review_status IN ({','.join('?' * len(values))})", values)
    # A record without a score fails every score bound.
    if score_min is not None:
        where = where.also("score >= ?", [score_min])
    if score_max is not None:
        where = where.also("score <= ?", [score_max])
    return where


def bbox_clause(bbox: str) -> tuple[str, list]:
    """R-tree candidates by overlap (its coordinates are rounded outwards), then the exact position. A box
    crossing the antimeridian (min lon > max lon) is split in two."""
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(422, "bbox must contain four numbers: minLon,minLat,maxLon,maxLat")
    if not (all(math.isfinite(v) for v in (min_lon, min_lat, max_lon, max_lat))
            and -180 <= min_lon <= 180 and -180 <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
        raise HTTPException(422, "bbox must have valid longitude and latitude bounds, with minLat <= maxLat")
    spans = [(min_lon, max_lon)] if min_lon <= max_lon else [(min_lon, 180.0), (-180.0, max_lon)]
    parts, params = [], []
    for lo, hi in spans:
        parts.append("(rid IN (SELECT rid FROM boreholes_rtree WHERE max_lat >= ? AND min_lat <= ? AND max_lon >= ? "
                     "AND min_lon <= ?) AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?)")
        params += [min_lat, max_lat, lo, hi, min_lat, max_lat, lo, hi]
    return "(" + " OR ".join(parts) + ")", params


def _search_words(q: str) -> list[str]:
    words = list(dict.fromkeys(re.findall(r"\w+", q, flags=re.UNICODE)))
    if len(words) > MAX_SEARCH_WORDS:
        raise HTTPException(422, f"a search can hold at most {MAX_SEARCH_WORDS} different words")
    return words


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _conditions(values: list[str], name: str) -> list[tuple[str, float | None, float | None]]:
    """'key', 'key:min:max', 'key:min:' or 'key::max' -> (key, min, max)."""
    out = []
    for value in values:
        key, *bounds = value.split(":")
        if not key or len(bounds) > 2:
            raise HTTPException(422, f"{name}: expected key or key:min:max, got {value!r}")
        try:
            lo, hi = [float(b) if b else None for b in [*bounds, "", ""][:2]]
        except ValueError:
            raise HTTPException(422, f"{name}: bounds must be numbers, got {value!r}") from None
        out.append((key, lo, hi))
    return out


def list_filters(
    q: str | None = None,
    lithology: list[str] = Query(default_factory=list, max_length=MAX_CONDITIONS,
                                 description="code[:from_m:to_m], e.g. clay:10:20 (a clay layer between 10 and 20 m)"),
    lab: list[str] = Query(default_factory=list, max_length=MAX_CONDITIONS,
                           description="parameter[:min:max], e.g. ucs:80: (UCS of at least 80)"),
    status: StatusFilter = "all",
    group: str | None = Query(None, description="only this document group (see the 'groups' facet)"),
    rock_head_max: float | None = Query(None, description="rock head at most this deep (m)"),
    groundwater_max: float | None = Query(None, description="groundwater at most this deep (m)"),
    material: list[str] = Query(default_factory=list, max_length=MAX_CONDITIONS,
                                description="group:from_m:to_m:metres, e.g. sand:0:20:5"),
    score_min: float | None = Query(None, ge=0, le=1, description="lowest release score"),
    score_max: float | None = Query(None, ge=0, le=1, description="highest release score"),
) -> Where:
    where = release_filters(status, score_min, score_max)
    if group:
        where = where.also("grp = ?", [group])
    if q and (tokens := _search_words(q)):
        # Text match, or every search word appears in the source document's file path (e.g. 'report').
        path_match = " AND ".join("path LIKE ? ESCAPE '\\'" for _ in tokens)
        where = where.also(
            "(rid IN (SELECT rowid FROM boreholes_fts WHERE boreholes_fts MATCH ?) "
            f"OR doc_id IN (SELECT doc_id FROM documents WHERE {path_match}))",
            [" AND ".join(f'"{t}"*' for t in tokens), *(f"%{_like_escape(t)}%" for t in tokens)])
    # Set lookups through the (code, depth) and (parameter, value) indexes. A correlated EXISTS per hole
    # would make SQLite scan every matching layer for every hole (171 s at 50,000 holes).
    for code, depth_from, depth_to in _conditions(lithology, "lithology"):  # each within its depth range
        clause, params = "rid IN (SELECT rid FROM layers WHERE primary_code = ?", [code]
        if depth_to is not None:
            clause += " AND top_m < ?"
            params.append(depth_to)
        if depth_from is not None:
            clause += " AND bottom_m > ?"
            params.append(depth_from)
        where = where.also(clause + ")", params)
    for parameter, lab_min, lab_max in _conditions(lab, "lab"):  # each measured at least once within its range
        clause, params = "rid IN (SELECT rid FROM lab_values WHERE parameter = ?", [parameter]
        if lab_min is not None:
            clause += " AND (qualifier IS NULL OR qualifier IN ('>', '≥', '>=', 'range')) AND value >= ?"
            params.append(lab_min)
        if lab_max is not None:
            clause += (" AND ((qualifier IS NULL OR qualifier IN ('<', '≤', '<=')) AND value <= ? "
                       "OR qualifier = 'range' AND value_upper >= value AND value_upper <= ?)")
            params += [lab_max, lab_max]
        where = where.also(clause + ")", params)
    try:
        materials = similar.material_conditions(material)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    ground = Where(*similar.ground_filters(rock_head_max, groundwater_max, materials))
    return Where([*where.clauses, *ground.clauses], [*where.params, *ground.params])


ListFilters = Annotated[Where, Depends(list_filters)]
