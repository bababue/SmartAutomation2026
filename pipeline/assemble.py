"""Turn per-page extractions into canonical `Borehole` records.

- Sheets of the same hole within a document are merged (continuation layers joined).
- A sheet that prints another position, ground level or investigation (report number, project, date) than
  the earlier sheets of its name goes to review, and is a hole of its own when its depths restart at the surface.
- Coordinates from summary tables and rows of lab sheets are linked to the holes by name.
- A coordinate system stated elsewhere in the document (other holes, tables, notes, report text) is used
  when a log does not name one; positions far from the other holes of a document are flagged.
- The same hole in several documents becomes one record (see `dedupe`).
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable
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
    Layer,
    Lithology,
    Sample,
    SecondaryComponent,
    SourceRef,
)

from . import corrections, injection, store
from .dedupe import dedupe, unique_groundwater
from .extraction_models import PageExtraction, PageLabResults, XCoordinateRow, XLog
from .georef import candidates, crs_from_hint, georeference, haversine_km, is_precise
from .identity import (ELEVATION_TOLERANCE_M, POSITION_TOLERANCE_M, HoleKey, document_id, hole_key, id_key, log_key,
                       lookup_hole, norm_name, same_header_text, same_id)
from .knowledge import printed_pair
from .labs import add_lab_result, lab_result, lab_value, plausible
from .scoring import counts_against, score

LOG_KINDS = {"borehole_log", "test_pit_log", "probe_log"}
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


def _unique_ids(records: list[Borehole]) -> None:
    """Holes whose plain IDs collide ('BH1-01' and 'BH10-1' → 'BH101') keep their number groups apart."""
    seen = Counter(bh.borehole_id for bh in records)
    for bh in records:
        if seen[bh.borehole_id] > 1:
            bh.borehole_id = f"{document_id(bh.borehole_id)}:{id_key(bh.borehole_name, separate_numbers=True)}"


_ID_PREFIX = {"test_pit": "TP", "other": "X"}


def _unique_ids_by_type(records: list[Borehole]) -> None:
    """Borehole '2', test pit '2' and probing '2' of one report: all but the borehole get a type prefix. Storing
    an id a second time replaces the first record, so an id still shared gets the record's first page and count."""
    seen = Counter(bh.borehole_id for bh in records)
    for bh in records:
        prefix = _ID_PREFIX.get(bh.investigation_type.value if bh.investigation_type else "")
        if seen[bh.borehole_id] > 1 and prefix:
            doc_id, rest = bh.borehole_id.split(":", 1)
            bh.borehole_id = f"{doc_id}:{prefix}{rest}"
    taken, kept = {bh.borehole_id for bh in records}, set()
    for bh in records:
        if bh.borehole_id in kept:
            page = bh.sources[0].page if bh.sources else None
            base, n = bh.borehole_id + (f"-p{page}" if page else ""), 1
            while (new := base + (f"-{n}" if n > 1 else "")) in taken:
                n += 1
            bh.borehole_id = new
            taken.add(new)
        kept.add(bh.borehole_id)


def _date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


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
        where = lambda x, y: (f"'{x}' on page {prev.source.page if prev.source else '?'}, "
                              f"'{y}' on page {nxt.source.page if nxt.source else '?'}")
        if field == "secondary":
            known = {s.lithology: s for s in a}
            for s in b:
                if (mine := known.get(s.lithology)) is None or s.qualifier is None:
                    continue
                if mine.qualifier is None:
                    mine.qualifier = s.qualifier
                elif mine.qualifier != s.qualifier:
                    conflicts.append(f"{s.lithology.value} qualifier {where(mine.qualifier.value, s.qualifier.value)}")
            prev.secondary = a + [s for s in b if s.lithology not in known]
        elif a is None:
            setattr(prev, field, b)
        else:
            conflicts.append(f"{field} {where(a, b)}")
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
        surface_assumed = i == 0 and tops[i] is None
        if tops[i] is None:
            tops[i] = bottoms[i - 1] if i > 0 else 0.0
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
        if surface_assumed and bottoms[i] is not None:  # a layer without either depth is dropped below
            notes.append("boundary inferred: top of the first layer not readable, set to 0 m")
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


OUTLIER = "km from the other holes of this document"


def flag_position_outliers(boreholes: list[Borehole]) -> list[Borehole]:
    """Holes of one document lie close together; one far from the others hints at a misread or a wrong CRS.
    Returns the holes flagged now (a hole flagged before is not flagged twice)."""
    precise = [b for b in boreholes if is_precise(b.location)]
    if len(precise) < 3:
        return []
    lat = statistics.median(b.location.lat for b in precise)
    lon = statistics.median(b.location.lon for b in precise)
    dist = {b.borehole_id: haversine_km(b.location.lat, b.location.lon, lat, lon) for b in precise}
    typical = statistics.median(dist.values())
    flagged = []
    for b in precise:
        if dist[b.borehole_id] > max(20.0, 5 * typical) and not any(OUTLIER in i for i in b.issues):
            b.issues.append(f"position {dist[b.borehole_id]:.0f} {OUTLIER}")
            b.location.confidence = min(b.location.confidence or 0.0, 0.5)
            flagged.append(b)
    return flagged


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


SHARED_PLACE = "would share one position from the place name"


def unstack_place_positions(boreholes: list[Borehole], keep: Collection[str] = ()) -> None:
    """Several holes of one folder or upload batch at one position from the place name: the point is the
    place's centre, where none of them need be, and the map would stack distinct holes on one spot. Their
    position is left to a reviewer; a single hole keeps its approximate one. `keep`: records a reviewer
    decided or corrected."""
    stacks: dict[tuple, list[Borehole]] = defaultdict(list)
    for bh in boreholes:
        loc = bh.location
        if loc.method == GeorefMethod.geocoded_place and loc.lat is not None and loc.lon is not None:
            stacks[(store.document_group(bh.borehole_id), round(loc.lat, 6), round(loc.lon, 6))].append(bh)
    for holes in stacks.values():
        if len(holes) < 2:
            continue
        for bh in holes:
            if bh.borehole_id in keep:
                continue
            bh.location = bh.location.model_copy(update={"lat": None, "lon": None, "method": None,
                                                         "accuracy_m": None, "confidence": 0.0})
            bh.issues = [i for i in bh.issues if i != "position from place name only"]
            bh.issues.append(f"not georeferenced: {len(holes)} holes of this folder or upload batch {SHARED_PLACE}")
            corrections.rescore(bh)


FEET = re.compile(r"\b(?:ft|feet|foot)\b|^\s*'\s*$", re.I)
# The model reports a conversion from feet in its notes and may give the unit as "m" afterwards. In free text
# "foot"/"Fuß" also means the foot of a slope. An apostrophe is no unit here: the notes quote printed text in
# single quotes ('Bore Hole: 5').
NOTES_FEET = re.compile(r"(?:\b|(?<=\d))(?:ft|feet)\b|(?:\d\s*|\b(?:from|in|von|aus)\s+)(?:foot|fu(?:ß|ss))\b", re.I)


def unit_findings(xs: list[XLog]) -> list[str]:
    """The model converts feet to metres itself; a person checks that conversion once."""
    units = {x.depth_unit_raw.strip() for x in xs if x.depth_unit_raw and FEET.search(x.depth_unit_raw)}
    if not units and any(x.notes and NOTES_FEET.search(x.notes) for x in xs):
        units = {"stated in the notes"}
    return [f"depths printed in feet ({', '.join(sorted(units))}); check the conversion to metres"] if units else []


def _first(values: Iterable[T]) -> T | None:
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
    if log.project_id and ids and not any(same_id(i, log.project_id) for i in ids):
        found.append(f"report/project id '{log.project_id}' vs '{ids[0]}'")
    texts = [field.replace("_", " ") for field in ("project_name", "location_text", "client", "contractor")
             if (value := getattr(log, field)) and (others := [getattr(x, field) for x in earlier if getattr(x, field)])
             and not any(same_header_text(o, value) for o in others)]
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
            # Stated in the document, only not on this log: still releasable, just not above the threshold.
            location.confidence = min(location.confidence or 0.0, 0.9)
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
        groundwater=unique_groundwater(
            GroundwaterObservation(depth_m=g.depth_m, kind=g.kind, observed_on=_date(g.date), dry=g.dry,
                                   reference=g.reference, raw=g.raw, source=src)
            for x, src in logs for g in x.groundwater),
        lab_results=[lab_result(lab, v, lab.top_m, lab.bottom_m, src)
                     for x, src in logs for lab in x.lab_results
                     if (v := lab_value(lab.parameter, lab.value_raw, lab.unit_raw)) is not None
                     and plausible(lab.parameter, v)],
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
    `name` that failed to assemble or came back without layers, else a log page that came back short. Its id and
    name never equal a real hole's, so it cannot overwrite one."""
    what = "not extracted" if name is None or issue.startswith("log not extracted") else "not assembled"
    stem = f"{id_key(name)}-" if name else ""
    bh = Borehole(borehole_id=f"{doc_id}:{stem}p{page}-{what.replace(' ', '-')}",
                  borehole_name=f"{name or 'log'} {what}, {doc_id} p{page}",
                  sources=[SourceRef(file=source_file, page=page)])
    bh.issues = [issue] + bh.validate_log()
    score(bh, {}, 0)
    return bh


def _file_order(doc_id: str) -> list:
    """'x_4' before 'x_10'."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", doc_id) if t]


# A continuation sheet starts at most this far below where the previous sheet ends: SPT forms take a 0.45 m sample
# every 1.5 m, so a sheet ending with a sample leaves 1.05 m to the next one.
SHEET_GAP_M = 1.5


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
                key = log_key(log, r.kind)
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


def reassign_misnamed_sheets(groups: dict[HoleKey, list[tuple[XLog, SourceRef]]]) -> dict[HoleKey, list[str]]:
    """A handwritten hole number can lose a digit on one sheet ('B-4' for 'B-14'). Sheet 'k of n' moves to the
    hole whose sheet 'k-1 of n' comes right before it when no sheet of its own name counts n sheets, the depths
    continue and the drilling date agrees. Returns the findings per hole that received a sheet."""
    entries = sorted(((src.page, n, key, log, src) for key, logs in groups.items()
                      for n, (log, src) in enumerate(logs) if log.layers), key=lambda e: (e[0], e[1]))
    findings: dict[HoleKey, list[str]] = defaultdict(list)
    prev: tuple[HoleKey, XLog] | None = None
    for _, _, key, log, src in entries:
        sheet = _sheet_number(log)
        if prev and sheet and sheet[0] > 1 and prev[0] != key and prev[0][0] == key[0] \
                and _sheet_number(prev[1]) == (sheet[0] - 1, sheet[1]) \
                and not any((s := _sheet_number(x)) and s[1] == sheet[1] for x, _ in groups[key] if x is not log) \
                and not (log.start_date and prev[1].start_date and log.start_date != prev[1].start_date):
            target = groups[prev[0]]
            if _continues_sheet(target, log) and _header_difference(target, log, None, None) is None:
                groups[key] = [(x, s) for x, s in groups[key] if x is not log]
                if not groups[key]:
                    del groups[key]
                target.append((log, src))
                target.sort(key=lambda e: e[1].page)
                findings[prev[0]].append(f"sheet assigned by its numbering: page {src.page} names '{log.name}', "
                                         f"taken as sheet {sheet[0]} of {sheet[1]} of '{prev[1].name}'")
                key = prev[0]
        prev = (key, log)
    return findings


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
                groups[log_key(log, r.kind)].append((log, SourceRef(file=r.source_file, page=r.page)))
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
        reassigned = reassign_misnamed_sheets(groups)
        page_texts = {(r.source_file, r.page): r.text for r in pages}
        doc_records: list[Borehole] = []
        split_pages: Counter[int] = Counter()
        for key, named in groups.items():
            kind = key[0]
            for logs, findings, naming in split_holes(named, group_crs[group], doc_place):
                if naming == "named":
                    findings = findings + reassigned.get(key, [])
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
            bh.issues += injection.findings(page_texts)
            score(bh, page_texts, unreadable)
            boreholes.append(bh)
    return dedupe(boreholes) if deduplicate else boreholes


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

    def sampled_at(bh: Borehole, top: float | None, bottom: float | None) -> bool:
        return top is not None and any(
            abs(s.top_m - top) <= 0.05 and (bottom is None or s.bottom_m is None or abs(s.bottom_m - bottom) <= 0.05)
            for s in bh.samples)

    def find_hole(name: str, page_kind: str | None, depth: float | None, bottom: float | None) -> Borehole | None:
        kind, number = hole_key(name, page_kind)
        bh = lookup_hole(by_name, (kind, number))
        if bh is None and kind is None:  # same number for a borehole and a pit: the depth decides
            fits = [b for (k, n), b in by_name.items() if n == number and deep_enough(b, depth)]
            if len(fits) > 1:  # both deep enough: the hole whose log has a sample at exactly that depth
                fits = [b for b in fits if sampled_at(b, depth, bottom)]
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
            value = lab_value(row.parameter, row.value_raw, row.unit_raw)
            if value is None:
                continue  # no number (e.g. 'NP', '-')
            if not plausible(row.parameter, value):
                implausible += 1
                continue
            bh = None
            if row.hole_name:
                bh = find_hole(row.hole_name, page_kind, row.top_m, row.bottom_m)
                if bh is not None and not deep_enough(bh, row.top_m):
                    bh = None
            elif row.sample_name:  # sheets often put the hole id into the sample column
                bh = find_hole(row.sample_name, page_kind, row.top_m, row.bottom_m)
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
            add_lab_result(bh, lab_result(row, value, top, bottom, SourceRef(file=r.source_file, page=r.page)))
            linked += 1
    return linked, unmatched, implausible
