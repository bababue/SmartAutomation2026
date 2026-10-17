"""Extraction quality against human-checked records.

    python -m pipeline eval snapshot    # store approved records as the reference (data/eval/reference.json)
    python -m pipeline eval             # compare the model output (before corrections) with the reference
    python -m pipeline eval --corrected # compare the final values, reviewer corrections included
    python -m pipeline eval holdout --doc GROUP   # keep these document groups out of prompt tuning

The reference stays local (data/ is git-ignored): it holds confidential extracted values. A snapshot
adds to or updates the existing reference, so records checked earlier stay in it even when a later
run changes them and they drop back to review.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from schema.borehole import Borehole, ReviewStatus

from . import config, store
from .georef import haversine_km

REFERENCE_PATH = config.DATA_DIR / "eval" / "reference.json"
DEPTH_TOLERANCE_M = 0.1


def entry(bh: Borehole) -> dict:
    """The parts of a record the evaluation compares (final values)."""
    return {"doc_id": bh.borehole_id.split(":", 1)[0], **store.extracted_summary(bh)}


def _records(con: sqlite3.Connection, where: str = "", params: tuple = ()) -> dict[str, Borehole]:
    return {r["id"]: Borehole.model_validate_json(r["data"])
            for r in con.execute(f"SELECT id, data FROM boreholes {where}", params)}


def current_entries(con: sqlite3.Connection, corrected: bool = False) -> tuple[dict[str, dict], int]:
    """Entries to compare: the model output before reviewer corrections, or the final values. Returns the
    entries and how many records had no stored model output (saved before it was kept; final values used)."""
    entries, fallback = {}, 0
    for r in con.execute("SELECT id, doc_id, data, extracted FROM boreholes"):
        if not corrected and r["extracted"]:
            entries[r["id"]] = {"doc_id": r["doc_id"], **json.loads(r["extracted"])}
        else:
            entries[r["id"]] = entry(Borehole.model_validate_json(r["data"]))
            fallback += not corrected
    return entries, fallback


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"boreholes": {}}


def load_reference(path: Path = REFERENCE_PATH) -> dict[str, dict]:
    return _load(path)["boreholes"]


def load_holdout(path: Path = REFERENCE_PATH) -> set[str]:
    """Document groups kept out of prompt tuning: their scores show whether a change generalises."""
    return set(_load(path).get("holdout", []))


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**data, "created": datetime.now(timezone.utc).isoformat()},
                               indent=1, ensure_ascii=False))


def snapshot(con: sqlite3.Connection, path: Path = REFERENCE_PATH) -> tuple[int, int, int]:
    """Add approved records (corrected ones included) to the reference. Returns (added, updated, total)."""
    data = _load(path)
    reference = data["boreholes"]
    approved = _records(con, "WHERE review_status = ?", (ReviewStatus.approved.value,))
    added = sum(bid not in reference for bid in approved)
    updated = sum(bid in reference and reference[bid] != entry(bh) for bid, bh in approved.items())
    reference.update({bid: entry(bh) for bid, bh in approved.items()})
    _write(path, data)
    return added, updated, len(reference)


def set_holdout(groups: list[str], path: Path = REFERENCE_PATH) -> set[str]:
    data = _load(path)
    data["holdout"] = sorted(set(groups))
    _write(path, data)
    return set(data["holdout"])


def match_boundaries(reference: list[float], current: list[float], tol: float = DEPTH_TOLERANCE_M) -> int:
    """Reference boundaries with a current boundary within `tol`, each current one used at most once."""
    remaining = sorted(current)
    matched = 0
    for depth in sorted(reference):
        best = min(range(len(remaining)), key=lambda i: abs(remaining[i] - depth), default=None)
        if best is not None and abs(remaining[best] - depth) <= tol:
            remaining.pop(best)
            matched += 1
    return matched


def _boundaries(layers: list[list]) -> list[float]:
    return sorted({d for top, bottom, _ in layers for d in (top, bottom) if d is not None})


def _metres(layers: list[list]) -> float:
    return sum(b - t for t, b, _ in layers if t is not None and b is not None and b > t)


def lithology_metres(reference: list[list], current: list[list]) -> float:
    """Metres of the reference log where the current log has the same primary lithology at the same depth.
    Unlike the per-layer check, this does not drop layers a variant segmented differently."""
    same = 0.0
    for top, bottom, primary in reference:
        if top is None or bottom is None:
            continue
        for c_top, c_bottom, c_primary in current:
            if c_primary == primary and c_top is not None and c_bottom is not None:
                same += max(0.0, min(bottom, c_bottom) - max(top, c_top))
    return same


def compare_layers(reference: list[list], current: list[list], tol: float = DEPTH_TOLERANCE_M) -> dict:
    """Boundary matches, and for reference layers whose top and bottom both match a current layer,
    how many have the same primary lithology."""
    ref_b, cur_b = _boundaries(reference), _boundaries(current)
    pairs = 0
    same = 0
    for top, bottom, primary in reference:
        hit = next((c for c in current if c[0] is not None and c[1] is not None
                    and abs(c[0] - top) <= tol and abs(c[1] - bottom) <= tol), None)
        if hit is not None:
            pairs += 1
            same += hit[2] == primary
    return {"boundaries": len(ref_b), "boundaries_matched": match_boundaries(ref_b, cur_b, tol),
            "layers_matched": pairs, "lithology_same": same}


def match_lab(reference: list[list], current: list[list], tol: float = DEPTH_TOLERANCE_M) -> int:
    """Reference lab values found with the same parameter, value and sample depth, each current one once."""
    remaining = list(current)
    matched = 0
    for parameter, value, top in reference:
        hit = next((c for c in remaining if c[0] == parameter and abs(c[1] - value) <= max(1e-6, abs(value) * 1e-3)
                    and (top is None) == (c[2] is None) and (top is None or abs(c[2] - top) <= tol)), None)
        if hit is not None:
            remaining.remove(hit)
            matched += 1
    return matched


def position_error_m(reference: dict, current: dict) -> float | None:
    if None in (reference.get("lat"), reference.get("lon"), current.get("lat"), current.get("lon")):
        return None
    return haversine_km(reference["lat"], reference["lon"], current["lat"], current["lon"]) * 1000


@dataclass
class Score:
    boreholes: int = 0
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    boundaries: int = 0  # reference boundaries, missing records included
    boundaries_matched: int = 0
    boundaries_current: int = 0  # boundaries of the compared current records, for precision
    layers_matched: int = 0
    lithology_same: int = 0
    metres: float = 0.0  # reference log length, missing records included
    metres_same: float = 0.0
    position_errors_m: list[float] = field(default_factory=list)
    no_position: int = 0  # reference has a position, the current record has none (or is missing)
    crs_changed: int = 0
    depths: int = 0  # final depths compared (references from before the field was kept have none)
    depths_ok: int = 0
    groundwater: int = 0
    groundwater_matched: int = 0
    lab: int = 0
    lab_matched: int = 0
    corrected: int = 0  # compared records with reviewer corrections: those values are not the model's

    def add(self, other: Score) -> None:
        for name, value in vars(other).items():
            setattr(self, name, getattr(self, name) + value)


def _by_key(current: dict[str, dict]) -> dict[str, str]:
    """Current records by natural key; keys held by more than one record are left out (ambiguous)."""
    seen: dict[str, list[str]] = {}
    for bid, cur in current.items():
        if cur.get("key"):
            seen.setdefault(cur["key"], []).append(bid)
    return {key: bids[0] for key, bids in seen.items() if len(bids) == 1}


def compare(reference: dict[str, dict], current: dict[str, dict],
            corrected: set[str] = frozenset()) -> dict[str, Score]:
    """Scores per document; only documents in the reference are compared. A reference record is matched by
    id, else by natural key (a variant may name the hole differently)."""
    scores: dict[str, Score] = {}
    by_key = _by_key(current)
    used: set[str] = set()
    for bid, ref in reference.items():
        s = scores.setdefault(ref["doc_id"], Score())
        s.boreholes += 1
        s.boundaries += len(_boundaries(ref["layers"]))
        s.metres += _metres(ref["layers"])
        match = bid if bid in current else by_key.get(ref.get("key"))
        if match is None or match in used:
            s.missing.append(bid)
            s.no_position += ref.get("lat") is not None
            continue
        used.add(match)
        cur = current[match]
        layers = compare_layers(ref["layers"], cur["layers"])
        s.boundaries_matched += layers["boundaries_matched"]
        s.boundaries_current += len(_boundaries(cur["layers"]))
        s.layers_matched += layers["layers_matched"]
        s.lithology_same += layers["lithology_same"]
        s.metres_same += lithology_metres(ref["layers"], cur["layers"])
        error = position_error_m(ref, cur)
        if error is not None:
            s.position_errors_m.append(error)
        elif ref.get("lat") is not None:
            s.no_position += 1
        s.crs_changed += ref.get("crs_epsg") != cur.get("crs_epsg")
        if ref.get("depth_m") is not None and "depth_m" in cur:
            s.depths += 1
            s.depths_ok += cur["depth_m"] is not None and abs(cur["depth_m"] - ref["depth_m"]) <= DEPTH_TOLERANCE_M
        if "groundwater" in ref and "groundwater" in cur:
            s.groundwater += len(ref["groundwater"])
            s.groundwater_matched += match_boundaries(ref["groundwater"], cur["groundwater"])
        if "lab" in ref and "lab" in cur:
            s.lab += len(ref["lab"])
            s.lab_matched += match_lab(ref["lab"], cur["lab"])
        s.corrected += match in corrected
    for bid, cur in current.items():
        if cur["doc_id"] in scores and bid not in used:
            scores[cur["doc_id"]].extra.append(bid)
    return scores


def total(scores: dict[str, Score], docs: set[str] | None = None) -> Score:
    t = Score()
    for doc_id, s in scores.items():
        if docs is None or store.document_group(doc_id) in docs or doc_id in docs:
            t.add(s)
    return t


def pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "    –"


def _m(values: list[float], fn) -> str:
    return f"{fn(values):8.1f}" if values else "       –"


HEADER = (f"{'':<24} {'holes':>5} {'miss':>4} {'extra':>5} {'bounds':>7} {'prec':>7} {'lith':>7} {'lith m':>7} "
          f"{'med m':>8} {'max m':>8} {'no pos':>6} {'crs≠':>4} {'depth':>7} {'gw':>7} {'lab':>7} {'corr':>4}")

LEGEND = [
    f"bounds: reference layer boundaries matched within {DEPTH_TOLERANCE_M} m (missing records count as unmatched); "
    "prec: share of the model's boundaries that are in the reference.",
    "lith: same primary lithology on layers whose top and bottom both match; lith m: share of the reference "
    "log length with the same lithology at the same depth.",
    "depth: final depth within the tolerance; gw: groundwater levels found; lab: lab values with the same "
    "parameter, value and sample depth.",
    "extra: records of the same documents that are not in the reference (often not yet reviewed).",
    "corr: compared records with reviewer corrections (with --corrected they match the reference by construction).",
]


def line(name: str, s: Score) -> str:
    return (f"{name[:24]:<24} {s.boreholes:>5} {len(s.missing):>4} {len(s.extra):>5} "
            f"{pct(s.boundaries_matched, s.boundaries):>7} {pct(s.boundaries_matched, s.boundaries_current):>7} "
            f"{pct(s.lithology_same, s.layers_matched):>7} {pct(s.metres_same, s.metres):>7} "
            f"{_m(s.position_errors_m, statistics.median)} {_m(s.position_errors_m, max)} "
            f"{s.no_position:>6} {s.crs_changed:>4} {pct(s.depths_ok, s.depths):>7} "
            f"{pct(s.groundwater_matched, s.groundwater):>7} {pct(s.lab_matched, s.lab):>7} {s.corrected:>4}")


def report(scores: dict[str, Score], holdout: set[str] = frozenset()) -> str:
    rule = "-" * len(HEADER)
    lines = [HEADER.replace(" " * 24, f"{'document':<24}", 1), rule]
    lines += [line(doc_id, scores[doc_id]) for doc_id in sorted(scores)]
    lines += [rule, line("overall", total(scores))]
    if holdout:
        lines += [line("  tuning", total(scores, {store.document_group(d) for d in scores} - holdout)),
                  line("  holdout", total(scores, holdout))]
    lines += [""] + LEGEND
    missing = sorted(bid for s in scores.values() for bid in s.missing)
    if missing:
        lines.append("missing: " + ", ".join(missing))
    return "\n".join(lines)


def cmd_eval(args) -> None:
    if args.action == "holdout":
        print(f"holdout: {', '.join(sorted(set_holdout(args.doc or []))) or 'none'}")
        return
    con = store.connect()
    if args.action == "snapshot":
        added, updated, n = snapshot(con)
        print(f"reference: {added} added, {updated} updated, {n} records in {REFERENCE_PATH}")
        return
    reference = load_reference()
    if not reference:
        print("no reference yet: approve records in the review UI, then run `python -m pipeline eval snapshot`")
        return
    current, fallback = current_entries(con, corrected=args.corrected)
    corrected = {bid for bid in current if store.corrections_for(con, bid)}
    print(("final values, reviewer corrections included" if args.corrected
           else "model output before reviewer corrections") + "\n")
    print(report(compare(reference, current, corrected), load_holdout()))
    if fallback:
        print(f"\n{fallback} records were stored before the model output was kept and are compared with their "
              "final values; run `python -m pipeline rebuild` to fill it in.")
