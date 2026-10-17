"""The same hole in several documents (a log, its image export, a drawing) becomes one record: header values
are filled across them, and disagreements or uncertain matches go to review."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from schema.borehole import Borehole, GroundwaterObservation, ReviewStatus

from . import corrections, store
from .georef import haversine_km, is_precise
from .identity import (ELEVATION_TOLERANCE_M, POSITION_TOLERANCE_M, document_id, header_words, hole_key,
                       same_header_text, same_id)
from .labs import add_lab_result
from .scoring import SOURCE_CONFLICTS, STRUCTURAL, finding_severity


def unique_groundwater(observations: Iterable[GroundwaterObservation]) -> list[GroundwaterObservation]:
    """Header fields are repeated on every sheet; keep one entry per distinct observation."""
    seen: dict[tuple, GroundwaterObservation] = {}
    for g in observations:
        seen.setdefault((g.depth_m, g.kind, g.reference, g.dry, g.observed_on), g)
    return list(seen.values())


DEPTH_TOLERANCE_M = 0.05
# Findings that describe a record's layer sequence or its position travel with the layers or the position.
LAYER_FINDINGS = STRUCTURAL + ("no layers extracted", "layer ", "minor discontinuity", "core loss inferred",
                               "lithology of layer", "missing sheets", "page ", "depths printed in feet",
                               "properties differ")
POSITION_FINDINGS = corrections.GEOREF_ISSUES + ("coordinate system from reviewer knowledge",)


def _group(bh: Borehole) -> str:
    """The folder or upload batch: files put together by a person, so one hole number there is one hole."""
    return store.document_group(bh.borehole_id)


def _name_tokens(name: str) -> list[str]:
    return [t.lstrip("0") or "0" if t.isdigit() else t for t in re.findall(r"[^\W\d_]+|\d+", name.upper())]


def _short_names(boreholes: list[Borehole]) -> dict[str, str]:
    """Record id -> the hole number to match by, for a record whose name ends in the full name of a hole in
    another file of its group ('<project> B 42' and 'B 42'). The short name needs a prefix and a number, and
    only whole tokens count ('AB 42' is not 'B 42'). A record without layers names no hole to match: a placeholder's
    name ends in its page ('... p1')."""
    out = {}
    for bh in boreholes:
        mine = _name_tokens(bh.borehole_name)
        for other in filter(lambda r: r.layers, boreholes):
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
        if same_id(a.project_id, b.project_id):
            agree.append(f"report/project id '{a.project_id}'")
        else:
            differ.append(f"report/project id '{a.project_id}' vs '{b.project_id}'")
    texts = [(f.replace("_", " "), x, y) for f in ("project_name", "location_name", "client", "contractor")
             if (x := getattr(a, f)) and (y := getattr(b, f)) and header_words(x) and header_words(y)]
    agree += [f for f, x, y in texts if same_header_text(x, y)]
    differing = [f for f, x, y in texts if not same_header_text(x, y)]
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
    if same:
        pairs = zip(sorted(main.layers, key=lambda x: x.top_m), sorted(other.layers, key=lambda x: x.top_m))
        issues += [f"sources disagree on the lithology of layer {x.top_m:g}–{x.bottom_m:g} m: {x.primary.value} "
                   f"({_where(main)}) vs {y.primary.value} ({_where(other)})" for x, y in pairs if x.primary != y.primary]
    else:
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
    spt = new(main.spt, other.spt, lambda s: (s.depth_m, tuple(s.blows or ()), s.n_value, s.refusal, s.increment_mm))
    runs = new(main.core_runs, other.core_runs, lambda c: (c.top_m, c.bottom_m))
    main.samples, main.spt, main.core_runs = main.samples + samples, main.spt + spt, main.core_runs + runs
    water = unique_groundwater(main.groundwater + other.groundwater)
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
    records whose own sheets disagree or that hold no layers (a placeholder for a page that failed). `protected`
    maps the records a reviewer decided or corrected to what identifies that review (uid, correction ids): such a
    record keeps its id and takes in an unreviewed copy; two records reviewed independently of each other both
    stay. `judged` holds the records as they will be stored, for choosing the better-checked copy. Changed
    records are scored again."""
    protected, judged = protected or {}, judged or {}
    short = _short_names(boreholes)
    clusters: dict[str, list[dict]] = defaultdict(list)
    kept: list[dict] = []
    for bh in sorted(boreholes, key=lambda b: b.borehole_id):
        entry = {"bh": bh, "ids": {bh.borehole_id}, "docs": {document_id(bh)}, "changed": False}
        kept.append(entry)
        if not bh.layers or any(i.startswith(("sheet header differs", "same printed coordinates as")) for i in bh.issues):
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
            # Evidence as stored: a reviewer's corrected position can contradict the printed one.
            match = _match(judged.get(other.borehole_id, other), judged.get(bh.borehole_id, bh))
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
