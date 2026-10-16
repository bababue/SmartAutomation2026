"""Extraction quality against human-checked records.

    python -m pipeline eval snapshot    # store approved records as the reference (data/eval/reference.json)
    python -m pipeline eval             # compare the current database with the reference

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
    """The parts of a record the evaluation compares."""
    loc = bh.location
    return {
        "doc_id": bh.borehole_id.split(":", 1)[0],
        "layers": [[layer.top_m, layer.bottom_m, layer.primary.value] for layer in bh.layers],
        "lat": loc.lat,
        "lon": loc.lon,
        "crs_epsg": loc.crs_epsg,
    }


def _records(con: sqlite3.Connection, where: str = "", params: tuple = ()) -> dict[str, Borehole]:
    return {r["id"]: Borehole.model_validate_json(r["data"])
            for r in con.execute(f"SELECT id, data FROM boreholes {where}", params)}


def load_reference(path: Path = REFERENCE_PATH) -> dict[str, dict]:
    return json.loads(path.read_text())["boreholes"] if path.exists() else {}


def snapshot(con: sqlite3.Connection, path: Path = REFERENCE_PATH) -> tuple[int, int, int]:
    """Add approved records (corrected ones included) to the reference. Returns (added, updated, total)."""
    reference = load_reference(path)
    approved = _records(con, "WHERE review_status = ?", (ReviewStatus.approved.value,))
    added = sum(bid not in reference for bid in approved)
    updated = sum(bid in reference and reference[bid] != entry(bh) for bid, bh in approved.items())
    reference.update({bid: entry(bh) for bid, bh in approved.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"created": datetime.now(timezone.utc).isoformat(), "boreholes": reference},
                               indent=1, ensure_ascii=False))
    return added, updated, len(reference)


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


def position_error_m(reference: dict, current: dict) -> float | None:
    if None in (reference.get("lat"), reference.get("lon"), current.get("lat"), current.get("lon")):
        return None
    return haversine_km(reference["lat"], reference["lon"], current["lat"], current["lon"]) * 1000


@dataclass
class Score:
    boreholes: int = 0
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    boundaries: int = 0
    boundaries_matched: int = 0
    layers_matched: int = 0
    lithology_same: int = 0
    position_errors_m: list[float] = field(default_factory=list)
    no_position: int = 0  # reference has a position, the current record has none
    crs_changed: int = 0
    corrected: int = 0  # compared records with reviewer corrections: those values are not the model's

    def add(self, other: Score) -> None:
        for name, value in vars(other).items():
            setattr(self, name, getattr(self, name) + value)


def compare(reference: dict[str, dict], current: dict[str, dict],
            corrected: set[str] = frozenset()) -> dict[str, Score]:
    """Scores per document; only documents in the reference are compared."""
    scores: dict[str, Score] = {}
    for bid, ref in reference.items():
        s = scores.setdefault(ref["doc_id"], Score())
        s.boreholes += 1
        cur = current.get(bid)
        if cur is None:
            s.missing.append(bid)
            continue
        layers = compare_layers(ref["layers"], cur["layers"])
        s.boundaries += layers["boundaries"]
        s.boundaries_matched += layers["boundaries_matched"]
        s.layers_matched += layers["layers_matched"]
        s.lithology_same += layers["lithology_same"]
        error = position_error_m(ref, cur)
        if error is not None:
            s.position_errors_m.append(error)
        elif ref.get("lat") is not None:
            s.no_position += 1
        s.crs_changed += ref.get("crs_epsg") != cur.get("crs_epsg")
        s.corrected += bid in corrected
    for bid, cur in current.items():
        if cur["doc_id"] in scores and bid not in reference:
            scores[cur["doc_id"]].extra.append(bid)
    return scores


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "    –"


def _metres(values: list[float], fn) -> str:
    return f"{fn(values):8.1f}" if values else "       –"


def report(scores: dict[str, Score]) -> str:
    header = (f"{'document':<24} {'holes':>5} {'miss':>4} {'extra':>5} {'bounds':>7} {'lith':>7} "
              f"{'med m':>8} {'max m':>8} {'no pos':>6} {'crs≠':>4} {'corr':>4}")
    lines = [header, "-" * len(header)]
    total = Score()

    def line(name: str, s: Score) -> str:
        return (f"{name:<24} {s.boreholes:>5} {len(s.missing):>4} {len(s.extra):>5} "
                f"{_pct(s.boundaries_matched, s.boundaries):>7} {_pct(s.lithology_same, s.layers_matched):>7} "
                f"{_metres(s.position_errors_m, statistics.median)} {_metres(s.position_errors_m, max)} "
                f"{s.no_position:>6} {s.crs_changed:>4} {s.corrected:>4}")

    for doc_id in sorted(scores):
        lines.append(line(doc_id[:24], scores[doc_id]))
        total.add(scores[doc_id])
    lines += ["-" * len(header), line("overall", total), "",
              f"bounds: share of reference layer boundaries matched within {DEPTH_TOLERANCE_M} m; "
              "lith: same primary lithology on layers whose top and bottom both match.",
              "extra: records of the same documents that are not in the reference (often not yet reviewed).",
              "corr: compared records with reviewer corrections; they are re-applied after every run, so these "
              "records match the reference by construction."]
    if total.missing:
        lines.append("missing: " + ", ".join(sorted(total.missing)))
    return "\n".join(lines)


def cmd_eval(args) -> None:
    con = store.connect()
    if args.action == "snapshot":
        added, updated, total = snapshot(con)
        print(f"reference: {added} added, {updated} updated, {total} records in {REFERENCE_PATH}")
        return
    reference = load_reference()
    if not reference:
        print("no reference yet: approve records in the review UI, then run `python -m pipeline eval snapshot`")
        return
    current = {bid: entry(bh) for bid, bh in _records(con).items()}
    corrected = {bid for bid in current if store.corrections_for(con, bid)}
    print(report(compare(reference, current, corrected)))
