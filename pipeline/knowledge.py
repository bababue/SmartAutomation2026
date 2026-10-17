"""What reviewers teach the system beyond single records, kept as data instead of code.

Knowledge items have a scope (document, project, region = country, global), a kind and a source:
- derived:  learned from corrections, e.g. a confirmed coordinate system counts for the country
- review:   entered by a reviewer from the documents
- external: told by someone (phone call, e-mail, site knowledge); kept apart so exports can say so

Kinds:
- crs:  {"epsg": 32639}                      – coordinate system used in this scope
- term: {"term": "caliche", "primary": "limestone"} – a local word and its DIN 4023 class
- note: {"text": "..."}                      – anything else; shown to reviewers and to the extraction model

Local grids are not stored: they are fitted on every run from the positions reviewers set by hand
in the same document (2-D Helmert, affine from four points), so they improve with every correction.

Order on every run: extraction → knowledge → reviewer corrections (a person's correction wins).
Learned values stay suggestions (position confidence below the release threshold) until confirmed
three times.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3

from pyproj import Transformer

from schema.borehole import Borehole, GeorefMethod, Lithology

from . import corrections, store
from .georef import _apply, _pairs, haversine_km, parse_tokens, resolve_with

SCOPES = ("document", "project", "region", "global")
KINDS = ("crs", "term", "note")
CONFIRMED = 3  # confirmations before a learned coordinate system counts as stated


def norm_project(name: str | None) -> str | None:
    return re.sub(r"\W+", "", name.lower()) or None if name else None


def scope_keys(bh: Borehole) -> dict[str, str | None]:
    return {"document": store.document_group(bh.borehole_id), "project": norm_project(bh.project_name),
            "region": (bh.location.country or "").upper() or None, "global": None}


SPECIFICITY = {"document": 0, "project": 1, "region": 2, "global": 3}


def applicable(items: list[dict], bh: Borehole, project: str | None = None) -> list[dict]:
    """Items for this record, most specific scope first. `project` overrides the extracted project name
    (a reviewer corrected it), so knowledge of the wrong project is not applied."""
    keys = scope_keys(bh)
    if project is not None:
        keys["project"] = norm_project(project)
    hits = [k for k in items if k["scope_type"] == "global" or
            (keys.get(k["scope_type"]) is not None and k["scope_value"] == keys[k["scope_type"]])]
    return sorted(hits, key=lambda k: (SPECIFICITY[k["scope_type"]], -k["confirmations"]))


# --- local grids from reviewer positions -----------------------------------------------------------

def printed_pair(coords_raw: str | None) -> tuple[float, float] | None:
    """The two printed numbers as (easting-like, northing-like) by their labels, else in printed order."""
    if not coords_raw:
        return None
    tokens = [t for t in parse_tokens(coords_raw) if t.values]
    pairs = _pairs(tokens)
    if not pairs:
        return None
    north, east, _ = pairs[0]
    return east.values[0], north.values[0]


def _solve3(m: list[list[float]], v: list[float]) -> list[float]:
    """Gaussian elimination for a 3x3 system (normal equations of the affine fit)."""
    a = [row[:] + [x] for row, x in zip(m, v)]
    for i in range(3):
        pivot = max(range(i, 3), key=lambda r: abs(a[r][i]))
        a[i], a[pivot] = a[pivot], a[i]
        if abs(a[i][i]) < 1e-12:
            raise ValueError("degenerate control points")
        for r in range(3):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [x - f * y for x, y in zip(a[r], a[i])]
    return [a[i][3] / a[i][i] for i in range(3)]


def _helmert(ab: list[tuple[float, float]], xy: list[tuple[float, float]]) -> list[float]:
    """Similarity transform X = p a - q b + tx, Y = q a + p b + ty (closed-form least squares)."""
    n = len(ab)
    ma, mb = sum(a for a, _ in ab) / n, sum(b for _, b in ab) / n
    mx, my = sum(x for x, _ in xy) / n, sum(y for _, y in xy) / n
    den = sum((a - ma) ** 2 + (b - mb) ** 2 for a, b in ab)
    if den < 1e-9:
        raise ValueError("control points coincide")
    p = sum((a - ma) * (x - mx) + (b - mb) * (y - my) for (a, b), (x, y) in zip(ab, xy)) / den
    q = sum((a - ma) * (y - my) - (b - mb) * (x - mx) for (a, b), (x, y) in zip(ab, xy)) / den
    return [p, -q, mx - p * ma + q * mb, q, p, my - q * ma - p * mb]


def _affine(ab: list[tuple[float, float]], xy: list[tuple[float, float]]) -> list[float]:
    rows = [(a, b, 1.0) for a, b in ab]
    m = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    cx = _solve3(m, [sum(r[i] * x for r, (x, _) in zip(rows, xy)) for i in range(3)])
    cy = _solve3(m, [sum(r[i] * y for r, (_, y) in zip(rows, xy)) for i in range(3)])
    return [*cx, *cy]


def fit_grid(points: list[tuple[tuple[float, float], tuple[float, float]]], validate: bool = True) -> dict | None:
    """Fit printed (a, b) → WGS84 from control points: Helmert, affine from four points. Tries both
    axis orders; returns parameters, RMS residual in metres and point count, or None if inconsistent."""
    if len(points) < 2:
        return None
    lat0 = sum(p[1][0] for p in points) / len(points)
    lon0 = sum(p[1][1] for p in points) / len(points)
    local = f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m"
    to_local = Transformer.from_crs(4326, local, always_xy=True)
    xy = [to_local.transform(lon, lat) for _, (lat, lon) in points]
    best = None
    for swap in (False, True):
        ab = [(b, a) if swap else (a, b) for (a, b), _ in points]
        try:
            c = _affine(ab, xy) if len(points) >= 4 else _helmert(ab, xy)
        except ValueError:
            continue
        residuals = [(c[0] * a + c[1] * b + c[2] - x) ** 2 + (c[3] * a + c[4] * b + c[5] - y) ** 2
                     for (a, b), (x, y) in zip(ab, xy)]
        rms = math.sqrt(sum(residuals) / len(residuals))
        scale = math.sqrt(abs(c[0] * c[4] - c[1] * c[3]))  # metres per printed unit
        if not 0.2 <= scale <= 5:  # e.g. all control points at one spot: not a grid
            continue
        if best is None or rms < best["rms_m"]:
            best = {"swap": swap, "params": c, "rms_m": rms, "n": len(points), "lat0": lat0, "lon0": lon0,
                    "scale": scale}
    if not best or best["rms_m"] > 50:
        return None
    spread = max(math.dist(a, b) for a in xy for b in xy)
    if spread < 30:  # control points too close together to define a grid
        return None
    best["centre"] = (sum(a for (a, _), _ in points) / len(points), sum(b for (_, b), _ in points) / len(points))
    best["radius"] = max(math.dist(pair, best["centre"]) for pair, _ in points)
    if len(points) >= 3 and validate:  # predictive check: each point from a grid fitted to the others
        errors = []
        for i in range(len(points)):
            g = fit_grid(points[:i] + points[i + 1:], validate=False)
            if g is None:
                return {**best, "loo_m": None}
            lat, lon = grid_position(g, points[i][0])
            errors.append(haversine_km(lat, lon, *points[i][1]) * 1000)
        best["loo_m"] = max(errors)
    else:
        best["loo_m"] = None
    return best


def grid_position(grid: dict, pair: tuple[float, float]) -> tuple[float, float]:
    a, b = (pair[1], pair[0]) if grid["swap"] else pair
    c = grid["params"]
    x, y = c[0] * a + c[1] * b + c[2], c[3] * a + c[4] * b + c[5]
    local = f"+proj=aeqd +lat_0={grid['lat0']} +lon_0={grid['lon0']} +datum=WGS84 +units=m"
    lon, lat = Transformer.from_crs(local, 4326, always_xy=True).transform(x, y)
    return lat, lon


def document_grids(con: sqlite3.Connection, boreholes: list[Borehole]) -> dict[str, dict]:
    """One grid per document group, from the holes a reviewer positioned by hand."""
    by_id = {bh.borehole_id: bh for bh in boreholes}
    points: dict[str, list] = {}
    by_uid = {uid: bh for bh in boreholes if (uid := store.uid_for(con, bh))}
    for row in con.execute("SELECT target, uid, payload FROM corrections WHERE scope = 'borehole' AND kind = 'position' "
                           "ORDER BY id"):
        bh = by_id.get(row["target"]) or (by_uid.get(row["uid"]) if row["uid"] else None)
        pair = printed_pair(bh.location.coords_raw) if bh else None
        if pair:
            payload = json.loads(row["payload"])
            points.setdefault(store.document_group(bh.borehole_id), {})[bh.borehole_id] = (pair, (payload["lat"], payload["lon"]))
    return {g: grid for g, pts in points.items() if (grid := fit_grid(list(pts.values())))}


# --- applying knowledge ------------------------------------------------------------------------------

def _settle_position(bh: Borehole) -> None:
    bh.issues = [i for i in bh.issues if not i.startswith(corrections.GEOREF_ISSUES)
                 and not corrections.GEOREF_UNREADABLE.search(i)]


def apply(bh: Borehole, items: list[dict], grid: dict | None = None) -> list[str]:
    """Apply what applies to this record; returns notes for its findings."""
    notes: list[str] = []
    loc = bh.location
    doubtful = loc.method != GeorefMethod.manual and (loc.confidence or 0) < 0.9 and loc.coords_raw
    if doubtful:
        crs_items = sorted((k for k in items if k["kind"] == "crs"), key=lambda k: -k["confirmations"])
        for k in crs_items:
            epsg = int(k["payload"]["epsg"])
            if loc.lat is None or loc.lon is None:
                break  # nothing to cross-check a learned system against
            near = (loc.lat, loc.lon)
            c = resolve_with(loc.coords_raw, epsg, near)
            # The learned system must land near where the record already is (inferred CRS or named place).
            if c is None or haversine_km(c.lat, c.lon, *near) > max(25.0, (loc.accuracy_m or 0) / 1000 * 2):
                continue
            _apply(loc, c, loc.method if loc.method == GeorefMethod.linked_table else GeorefMethod.stated_coordinates)
            loc.crs_candidates, loc.accuracy_m = [], 10.0
            loc.confidence = 0.9 if k["confirmations"] >= CONFIRMED else 0.8
            _settle_position(bh)
            notes.append(f"coordinate system from reviewer knowledge: EPSG:{epsg} "
                         f"({k['confirmations']} confirmation(s), {k['scope_type']})")
            break
    pair = printed_pair(loc.coords_raw)
    inside = grid and pair and math.dist(pair, grid["centre"]) <= max(3 * grid["radius"], 1.0)  # no far extrapolation
    if inside and loc.method == GeorefMethod.geocoded_place:
        lat, lon = grid_position(grid, pair)
        if loc.lat is None or haversine_km(lat, lon, loc.lat, loc.lon) <= 50:
            loc.lat, loc.lon, loc.method = round(lat, 7), round(lon, 7), GeorefMethod.stated_coordinates
            loc.crs_epsg, loc.crs_candidates = None, []
            loc.crs_proj = f"local grid fitted to {grid['n']} reviewer positions (RMS {grid['rms_m']:.1f} m)"
            loc.accuracy_m = max(grid["rms_m"], 5.0)
            # Release only on a predictive check: every control point recovered from the others within 20 m.
            loc.confidence = 0.9 if grid["loo_m"] is not None and grid["loo_m"] <= 20 else 0.8
            _settle_position(bh)
            notes.append(f"position from a local grid fitted to {grid['n']} reviewer positions "
                         f"(RMS {grid['rms_m']:.1f} m)")
    terms = [k for k in items if k["kind"] == "term"]  # most specific scope first
    for layer in bh.layers:
        # The main material is named first ('CALICHE : grey, ...'); 'SAND, trace of caliche' stays sand.
        leading = re.split(r"[:,;(]", layer.description_original.lstrip("*- "), maxsplit=1)[0]
        for k in terms:
            term, target = k["payload"]["term"], Lithology(k["payload"]["primary"])
            pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.I)
            if layer.primary == Lithology.unknown and pattern.search(layer.description_original) or pattern.search(leading):
                if layer.primary != target:
                    layer.primary = target
                    notes.append(f"lithology of layer {layer.top_m:g}–{layer.bottom_m:g} m from reviewer knowledge: "
                                 f"'{term}' → {target.value}")
                break
    return notes


def apply_all(con: sqlite3.Connection, boreholes: list[Borehole]) -> None:
    """Knowledge on freshly assembled records (before the reviewer corrections are re-applied)."""
    items = store.active_knowledge(con)
    grids = document_grids(con, boreholes)
    if not items and not grids:
        return
    for bh in boreholes:
        fixed = [c["payload"]["value"] for c in store.corrections_for(con, bh.borehole_id, store.uid_for(con, bh))
                 if c["kind"] == "field" and c["payload"]["field"] == "project_name"]
        notes = apply(bh, applicable(items, bh, fixed[-1] if fixed else None),
                      grids.get(store.document_group(bh.borehole_id)))
        if notes:
            bh.issues = list(dict.fromkeys(bh.issues + notes))
            corrections.rescore(bh)


def prompt_context(con: sqlite3.Connection, doc_id: str) -> str:
    """Known facts for the extraction of a document's new pages ('' if none). Region and project come
    from earlier records of the document, if any."""
    group = doc_id.split("__")[0]
    rows = con.execute("SELECT country, project FROM boreholes WHERE doc_id = ? OR doc_id LIKE ? ESCAPE '\\'",
                       (group, group.replace("_", "\\_") + "\\_\\_%")).fetchall()
    keys = {"document": group, "global": None,
            "region": {(r["country"] or "").upper() for r in rows if r["country"]},
            "project": {norm_project(r["project"]) for r in rows if r["project"]}}
    lines = []
    for k in store.active_knowledge(con):
        scope = keys.get(k["scope_type"])
        hit = k["scope_type"] == "global" or (k["scope_value"] in scope if isinstance(scope, set) else k["scope_value"] == scope)
        if not hit:
            continue
        p, origin = k["payload"], "told by someone" if k["source"] == "external" else "from earlier reviews"
        if k["kind"] == "crs":
            continue  # applied in code with a place check; in the prompt it could end up as 'printed' crs_raw
        if k["kind"] == "term":
            lines.append(f"- '{p['term']}' means the DIN 4023 class {p['primary']} ({origin}).")
        elif k["kind"] == "note":
            lines.append(f"- {p['text']} ({origin})")
    if not lines:
        return ""
    return ("Known from reviewers for this document or region (use only if the page agrees; what is printed on the "
            "page wins; do not copy values that are not printed):\n" + "\n".join(lines[:15]))
