"""Second reading of printed coordinates: the region around a hole's coordinates is read once more, enlarged,
with a short verbatim-only prompt, and compared with the first reading after parsing. Coordinates weigh most
for the challenge, and a misread digit moves a hole without any other finding noticing it."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

from schema.borehole import Borehole, GeorefMethod, Location

from . import config
from .extraction_models import CoordinateReading, PageExtraction
from .georef import candidates, crs_from_hint, haversine_km, is_precise, resolve_with
from .identity import document_id, norm_name
from .ingest import PageImage

# Two readings of the same printed text agree to well below this after parsing; more is a different reading.
SAME_POINT_M = 5.0

CONFIRMED = "coordinates confirmed by a second reading"
NOT_FOUND = "coordinates not found on a second look"
DIFFERENT = "coordinates read differently on a second look"
DIFFERENT_CRS = "coordinate system read differently on a second look"

Key = tuple[str, int, str, str]  # doc_id, page, normalised hole name, "log" or "table"
Target = tuple[PageImage, tuple | None, str, str]  # page, coordinate box, hole name, "log" or "table"


def targets(pages: Iterable[PageImage], extractions: dict[tuple[str, int], PageExtraction]) -> Iterator[Target]:
    """(page, coordinate box, hole name, source) of every log with printed coordinates, and of the coordinate-table
    rows of holes whose logs print none (only those take the table's position). Table rows carry no box, so their
    whole page is read."""
    found = [(p, x) for p in pages if (x := extractions.get((p.doc_id, p.page))) is not None]
    without = {(p.doc_id, norm_name(log.name)) for p, x in found for log in x.logs if not log.coords_raw}
    for p, x in found:
        for log in x.logs:
            if log.coords_raw:
                bbox = tuple(log.coords_bbox) if log.coords_bbox and len(log.coords_bbox) == 4 else None
                yield p, bbox, log.name, "log"
        for row in x.coordinate_rows:
            if row.coords_raw and (p.doc_id, norm_name(row.name)) in without:
                yield p, None, row.name, "table"


def readings(items: Iterable[Target], read: Callable[[PageImage, tuple | None, str], CoordinateReading | None]
             ) -> dict[Key, CoordinateReading | None]:
    """read: a model call or a cache lookup. A failed reading leaves its hole without a second look; two holes of
    one name on one page are ambiguous (None) and get none either."""
    items = list(items)
    out: dict[Key, CoordinateReading | None] = {}
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = {pool.submit(read, p, bbox, hole): (p, hole, source) for p, bbox, hole, source in items}
        for fut in as_completed(futures):
            p, hole, source = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:  # keep going; the record simply has no second reading
                print(f"  ! second coordinate reading failed {p.doc_id} p{p.page} {hole}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                continue
            if r is not None:
                key = (p.doc_id, p.page, norm_name(hole), source)
                out[key] = None if key in out else r
    return out


def _reading_for(bh: Borehole, found: dict[Key, CoordinateReading | None]) -> CoordinateReading | None:
    doc_id, name, src = document_id(bh), norm_name(bh.borehole_name), bh.location.source
    if bh.location.method == GeorefMethod.linked_table:
        rows = [r for (d, _, n, source), r in found.items() if d == doc_id and n == name and source == "table"]
        return rows[0] if len(rows) == 1 else None
    if src is None or src.page is None:
        return None
    return found.get((doc_id, src.page, name, "log"))


def _equivalent(loc: Location, crs: int | str) -> bool:
    """The first reading's text gives the same point in `crs` (e.g. ETRS89 and WGS84 UTM of one zone)."""
    if not isinstance(crs, int) or not loc.coords_raw:
        return False
    c = resolve_with(loc.coords_raw, crs, (loc.lat, loc.lon))
    return c is not None and haversine_km(loc.lat, loc.lon, c.lat, c.lon) * 1000 <= SAME_POINT_M


def check(bh: Borehole, found: dict[Key, CoordinateReading | None]) -> str | None:
    """The finding for a record whose position comes from printed coordinates, None without a second reading."""
    loc = bh.location
    if not is_precise(loc):
        return None
    reading = _reading_for(bh, found)
    if reading is None:
        return None
    if not reading.coords_raw:
        return NOT_FOUND
    first_crs = loc.crs_epsg if loc.crs_epsg is not None else loc.crs_proj
    cands = candidates(reading.coords_raw, reading.crs_raw or loc.crs_raw, loc.country, (loc.lat, loc.lon))
    in_first_crs = [c for c in cands if (c.epsg if c.epsg is not None else c.crs) == first_crs]
    if loc.crs_epsg is not None and (same := resolve_with(reading.coords_raw, loc.crs_epsg, (loc.lat, loc.lon))):
        in_first_crs.append(same)
    in_first_crs = in_first_crs or cands
    distance_m = min((haversine_km(loc.lat, loc.lon, c.lat, c.lon) * 1000 for c in in_first_crs), default=None)
    if distance_m is not None and distance_m <= SAME_POINT_M:
        return CONFIRMED
    hinted = crs_from_hint(reading.crs_raw) + crs_from_hint(reading.coords_raw)
    if hinted and first_crs is not None and first_crs not in hinted and not any(_equivalent(loc, c) for c in hinted):
        return f"{DIFFERENT_CRS}: '{reading.crs_raw}'"
    if distance_m is None:
        return f"{DIFFERENT}: '{reading.coords_raw}' could not be parsed"
    return f"{DIFFERENT}: '{reading.coords_raw}', {distance_m:,.0f} m from the first reading"
