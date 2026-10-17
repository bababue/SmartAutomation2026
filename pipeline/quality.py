"""Extraction quality in aggregates for the statistics page: the current database and every experiment arm
against the reviewed reference, the models side by side with their speed and cost, and the wider benchmark.

    python -m pipeline eval snapshot-models   # write them to fixtures/model_evaluation.json

The API computes them from data/eval where it exists and serves the committed snapshot elsewhere (the
Docker image has no data/). Nothing here holds a record value, an id or a document name: shares, counts,
seconds and dollars only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import config, evaluate, experiment, llm, runner, store
from .assemble import LOG_KINDS

SNAPSHOT_PATH = config.ROOT / "fixtures" / "model_evaluation.json"

# Transcribed from docs/model-comparison.md (wider benchmark): 6 log pages from 4 documents, same prompt,
# compared with Opus. A pair is a range over repeated runs or, for the local model, two runtimes.
# `compared`: Opus's values on those pages, the base of each share.
BENCHMARK = {
    "pages": 6, "documents": 4, "against": "claude-opus-5-5",
    "compared": {"boundaries": 37, "final_depth": 4, "groundwater": 3, "lab": 4},
    "rows": [
        {"model": "Sonnet 5.5", "route": "OpenRouter", "seconds_per_page": [28, 28], "usd_per_page": 0.069,
         "boundaries": [1.0, 1.0], "precision": [1.0, 1.0], "lithology": [0.84, 0.84], "final_depth": [1.0, 1.0],
         "groundwater": [0.67, 0.67], "lab": [0.0, 0.0]},
        {"model": "Gemini 4 Argon", "route": "OpenRouter", "seconds_per_page": [30, 30], "usd_per_page": 0.035,
         "boundaries": [0.86, 0.86], "precision": [0.86, 0.86], "lithology": [0.95, 0.95], "final_depth": [1.0, 1.0],
         "groundwater": [1.0, 1.0], "lab": [0.5, 0.5]},
        {"model": "GPT-6.1 Sol", "route": "OpenRouter", "seconds_per_page": [44, 44], "usd_per_page": 0.039,
         "boundaries": [0.97, 0.97], "precision": [0.97, 0.97], "lithology": [0.89, 0.89], "final_depth": [1.0, 1.0],
         "groundwater": [1.0, 1.0], "lab": [0.25, 0.25]},
        {"model": "Haiku 5.5", "route": "OpenRouter", "seconds_per_page": [27, 27], "usd_per_page": 0.0046,
         "boundaries": [1.0, 1.0], "precision": [1.0, 1.0], "lithology": [0.85, 0.85], "final_depth": [1.0, 1.0],
         "groundwater": [1.0, 1.0], "lab": [0.0, 0.0]},
        {"model": "Gemini 3.8 Flash", "route": "OpenRouter", "seconds_per_page": [32, 32], "usd_per_page": 0.026,
         "boundaries": [0.86, 0.86], "precision": [0.94, 0.94], "lithology": [0.88, 0.88], "final_depth": [1.0, 1.0],
         "groundwater": [1.0, 1.0], "lab": [0.5, 0.5]},
        {"model": "GPT-6 Luna", "route": "OpenRouter", "seconds_per_page": [29, 29], "usd_per_page": 0.0025,
         "boundaries": [0.81, 0.81], "precision": [0.81, 0.81], "lithology": [0.77, 0.77], "final_depth": [1.0, 1.0],
         "groundwater": [0.67, 0.67], "lab": [0.0, 0.0]},
        {"model": "Qwen3-VL 8B, 4 bit", "route": "local (Apple M4, 24 GB)", "seconds_per_page": [140, 245], "usd_per_page": 0.0,
         "boundaries": [0.3, 0.54], "precision": [0.61, 0.85], "lithology": [0.09, 0.52], "final_depth": [0.75, 1.0],
         "groundwater": [0.33, 0.67], "lab": [0.0, 0.0]},
    ],
}


def read_only(path: Path) -> sqlite3.Connection:
    """Arm databases may belong to a running experiment, and the snapshot must never change a database."""
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _share(hit: float, of: float) -> dict:
    return {"hit": round(hit, 2), "of": round(of, 2)}


def metrics(s: evaluate.Score) -> dict:
    errors = s.position_errors_m
    return {"records": s.boreholes, "missing": len(s.missing), "corrected": s.corrected,
            "boundaries": _share(s.boundaries_matched, s.boundaries),
            "precision": _share(s.boundaries_matched, s.boundaries_current),
            "lithology_depth": _share(s.metres_same, s.metres),
            "final_depth": _share(s.depths_ok, s.depths),
            "groundwater": _share(s.groundwater_matched, s.groundwater),
            "lab": _share(s.lab_matched, s.lab),
            "position_median_m": round(statistics.median(errors), 1) if errors else None,
            "position_max_m": round(max(errors), 1) if errors else None,
            "without_position": s.no_position, "crs_changed": s.crs_changed}


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def run_stats(pages: list[dict]) -> dict:
    """Model, speed, tokens and cost per page of an arm's extraction cache. Cost is as billed or priced from
    tokens where the backend reported usage on every page, else the pipeline's estimate for the model."""
    models = Counter(p["model"] for p in pages if p.get("model"))
    model = models.most_common(1)[0][0] if models else None
    usages = [p["usage"] for p in pages if p.get("usage")]
    tokens = {f: [sum(t.get(f, 0) for t in u.values()) for u in usages] for f in ("input_tokens", "output_tokens")}
    costs = [c for u in usages if (c := llm.cost_usd(u)) is not None]
    measured = bool(pages) and len(costs) == len(pages)
    usd = (statistics.fmean(costs) if measured
           else runner.page_cost(runner.COST_EXTRACT, model) if model else None)
    return {"pages": len(pages), "model": model,
            "seconds_per_page": _median([p["seconds"] for p in pages if p.get("seconds") is not None]),
            "input_tokens": _median(tokens["input_tokens"]), "output_tokens": _median(tokens["output_tokens"]),
            "usd_per_page": round(usd, 5) if usd is not None else None, "usd_measured": measured}


def _cache_pages(arm: Path) -> dict[tuple[str, int], dict]:
    """The arm's extraction cache by (document, page); a page may have several entries, one per hole."""
    pages = {}
    for path in sorted((arm / "cache" / "extract").glob("*/*.json")):
        pages.setdefault((path.parent.name, int(path.stem.split("-")[0])), json.loads(path.read_text(encoding="utf-8")))
    return pages


def _prompt(arm: Path) -> str | None:
    path = arm / "prompt.md"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.exists() else None


def _arm(name: str) -> dict:
    """An arm's records in evaluation form, the pages it extracted itself (None: all, from the main cache)
    and its run statistics."""
    path = experiment.arm_dir(name)
    con = read_only(path / "boreholes.sqlite")
    try:
        entries, _ = evaluate.current_entries(con)
    finally:
        con.close()
    pages = _cache_pages(path)
    modified = datetime.fromtimestamp((path / "boreholes.sqlite").stat().st_mtime, timezone.utc)
    return {"name": name, "date": modified.date().isoformat(), "prompt": _prompt(path),
            **run_stats(list(pages.values())), "entries": entries, "fresh": set(pages) or None,
            "docs": {e["doc_id"] for e in entries.values()}}


def record_pages(con: sqlite3.Connection, ids: list[str]) -> dict[str, set[tuple[str, int]]]:
    """(document, page) of the log pages each record was read from, per the given database."""
    out: dict[str, set[tuple[str, int]]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in con.execute(
                "SELECT b.id, d.doc_id, s.page FROM record_sources s JOIN boreholes b ON b.rid = s.rid "
                f"JOIN documents d ON d.path = s.file WHERE s.role = 'log' AND b.id IN ({','.join('?' * len(chunk))})",
                chunk):
            out.setdefault(r["id"], set()).add((r["doc_id"], r["page"]))
    return out


def _covered(arm: dict, reference: dict[str, dict], pages: dict[str, set]) -> set[str]:
    """Reference records whose log pages this arm extracted itself. Arms re-extract only some pages and take
    the rest from the main cache, which the reference was approved from: those records would favour the arm."""
    if arm["fresh"] is None:
        return {bid for bid, ref in reference.items() if bid in pages and ref["doc_id"] in arm["docs"]}
    return {bid for bid in reference if bid in pages and pages[bid] <= arm["fresh"]}


def _scored(arm: dict, reference: dict[str, dict], ids: set[str]) -> dict:
    return metrics(evaluate.total(evaluate.compare({b: reference[b] for b in ids}, arm["entries"])))


RUN_FIELDS = ("pages", "model", "seconds_per_page", "input_tokens", "output_tokens", "usd_per_page", "usd_measured")


def prompt_arms(arms: list[dict], reference: dict[str, dict], pages: dict[str, set]) -> dict:
    """Every arm on the same records: the reference records all arms extracted themselves."""
    covered = {a["name"]: _covered(a, reference, pages) for a in arms}
    common = set.intersection(*covered.values()) if arms else set()
    return {"records": len(common), "documents": len({reference[b]["doc_id"] for b in common}),
            "runs": [{"name": a["name"], "date": a["date"], "prompt": a["prompt"], "covered": len(covered[a["name"]]),
                      **{k: a[k] for k in RUN_FIELDS}, "metrics": _scored(a, reference, common)} for a in arms]}


def model_comparison(arms: list[dict], reference: dict[str, dict], pages: dict[str, set]) -> dict | None:
    """The newest arms that ran one prompt with different models, so only the model differs, on the reference
    records both extracted themselves."""
    groups: dict[str, list[dict]] = {}
    for arm in arms:
        if arm["prompt"] and arm["model"] and arm["pages"]:
            groups.setdefault(arm["prompt"], []).append(arm)
    candidates = [g for g in groups.values() if len({a["model"] for a in g}) > 1]
    if not candidates:
        return None
    group = max(candidates, key=lambda g: max(a["date"] for a in g))
    newest = {a["model"]: a for a in sorted(group, key=lambda a: a["date"])}
    runs = sorted(newest.values(), key=lambda a: -(a["usd_per_page"] or 0))
    common = set.intersection(*(_covered(a, reference, pages) for a in runs))
    return {"prompt": group[0]["prompt"], "records": len(common),
            "runs": [{"arm": a["name"], **{k: a[k] for k in RUN_FIELDS}, "metrics": _scored(a, reference, common)}
                     for a in runs]}


_memo: dict[str, tuple[tuple, object]] = {}


def _cached(name: str, paths: list[Path], compute: Callable[[], object]):
    """compute() again only when one of the files changed; scoring every arm takes a few seconds."""
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (str(p),) for p in paths)
    hit = _memo.get(name)
    if hit is None or hit[0] != key:
        hit = _memo[name] = (key, compute())
    return hit[1]


def _db_files(path: Path) -> list[Path]:
    return [path, path.with_name(path.name + "-wal")]


def _current(con: sqlite3.Connection, reference: dict[str, dict]) -> dict:
    current, _ = evaluate.current_entries(con)
    corrected = {bid for bid in current if store.corrections_for(con, bid)}
    return metrics(evaluate.total(evaluate.compare(reference, current, corrected)))


def log_pages(con: sqlite3.Connection) -> int:
    kinds = sorted(LOG_KINDS)
    return con.execute(f"SELECT COUNT(*) FROM pages WHERE kind IN ({', '.join('?' * len(kinds))})", kinds).fetchone()[0]


def evaluation(con: sqlite3.Connection) -> dict | None:
    """Everything from data/eval and the given database; None without a reference."""
    ref_path = evaluate.REFERENCE_PATH
    reference = evaluate.load_reference(ref_path)
    if not reference:
        return None
    created = json.loads(ref_path.read_text(encoding="utf-8")).get("created", "")[:10] or None
    names = experiment.arms()
    arm_files = [ref_path] + [f for n in names for f in _db_files(experiment.arm_dir(n) / "boreholes.sqlite")]
    arms = _cached("arms", arm_files, lambda: [_arm(n) for n in names])
    db_files = _db_files(config.DB_PATH)
    pages = _cached("pages", [ref_path, *db_files], lambda: record_pages(con, list(reference)))
    scored = _cached("scored", arm_files + db_files,
                     lambda: (model_comparison(arms, reference, pages), prompt_arms(arms, reference, pages)))
    current = _cached("current", [ref_path, *db_files], lambda: _current(con, reference))
    return {"generated": datetime.now(timezone.utc).date().isoformat(),
            "reference": {"records": len(reference), "documents": len(experiment.reference_docs(reference)),
                          "created": created},
            "current": current, "models": scored[0], "arms": scored[1], "benchmark": BENCHMARK,
            "log_pages": log_pages(con)}


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_snapshot(path: Path = SNAPSHOT_PATH) -> dict | None:
    con = read_only(config.DB_PATH)
    try:
        data = evaluation(con)
    finally:
        con.close()
    if data is not None:
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def usage(con: sqlite3.Connection) -> list[dict]:
    """Model calls recorded in this database per model, backend and stage (cache hits make none)."""
    return [dict(r) for r in con.execute(
        "SELECT model, endpoint, stage, COUNT(*) calls, SUM(outcome != 'ok') errors, "
        "COUNT(DISTINCT doc_id || ':' || page) pages, COALESCE(SUM(input_tokens), 0) input_tokens, "
        "COALESCE(SUM(output_tokens), 0) output_tokens, SUM(cost_usd) cost_usd, COUNT(cost_usd) priced, "
        "COALESCE(SUM(seconds), 0) seconds, MIN(at) first, MAX(at) last "
        "FROM llm_calls GROUP BY model, endpoint, stage ORDER BY calls DESC")]
