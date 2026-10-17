"""Prompt experiments: try an extraction prompt on the reference documents without touching the main cache,
the main database or the reviewers' work.

    python -m pipeline experiment baseline          # the main cache into a fresh DB (free)
    python -m pipeline experiment init NAME         # data/eval/experiments/NAME/prompt.md = the current prompt
    python -m pipeline experiment run NAME --yes    # extract the reference documents with NAME's prompt
    python -m pipeline experiment compare           # every arm against the reference, per document

Each arm has its own extraction cache and a fresh database without corrections or knowledge, so the scores
are the model's alone. Classifications and lab sheets come from the main cache: a prompt experiment changes
extraction only. Model and effort come from the usual settings (EXTRACT_MODEL, EXTRACT_EFFORT) and are part
of the cache key. The main cache can hold pages first extracted with reviewer knowledge in the prompt, so
the baseline may be slightly favoured on documents that have knowledge entries.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import statistics
from pathlib import Path

from . import config, evaluate, llm, store

EXPERIMENTS_DIR = config.DATA_DIR / "eval" / "experiments"
BASELINE = "baseline"


def arm_dir(name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise SystemExit(f"experiment names are lowercase letters, digits, '-' and '_': {name!r}")
    return EXPERIMENTS_DIR / name


def init(name: str) -> Path:
    path = arm_dir(name) / "prompt.md"
    if path.exists():
        raise SystemExit(f"{path} exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(llm.EXTRACT_SYSTEM)
    return path


def use_prompt(name: str) -> str:
    """Point this process's extraction at the arm's prompt and cache. The prompt's hash replaces the prompt
    version in the extraction key, so an edited prompt.md never reuses stale pages."""
    path = arm_dir(name) / "prompt.md"
    if not path.exists():
        raise SystemExit(f"no prompt for {name}: run `python -m pipeline experiment init {name}` and edit {path}")
    prompt = path.read_text()
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    config.CACHE_DIR = arm_dir(name) / "cache"
    config.PROMPT_VERSION = f"exp-{fingerprint}"
    llm.EXTRACT_SYSTEM = prompt
    return fingerprint


def fresh_db(name: str) -> sqlite3.Connection:
    path = arm_dir(name) / "boreholes.sqlite"
    for f in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        f.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.DB_PATH = path
    return store.connect(path)


def arms() -> list[str]:
    found = sorted(p.parent.name for p in EXPERIMENTS_DIR.glob("*/boreholes.sqlite"))
    return sorted(found, key=lambda n: n != BASELINE)


def arm_scores(name: str, reference: dict[str, dict]) -> dict[str, evaluate.Score]:
    con = store.connect(arm_dir(name) / "boreholes.sqlite")
    try:
        current, _ = evaluate.current_entries(con)
    finally:
        con.close()
    return evaluate.compare(reference, current)


def compare_arms(reference_path: Path = evaluate.REFERENCE_PATH) -> str:
    reference = evaluate.load_reference(reference_path)
    if not reference:
        return "no reference yet: approve records in the review UI, then run `python -m pipeline eval snapshot`"
    names = arms()
    if not names:
        return "no experiment results yet: run `python -m pipeline experiment baseline` first"
    holdout = evaluate.load_holdout(reference_path)
    scores = {name: arm_scores(name, reference) for name in names}
    rule = "-" * len(evaluate.HEADER)
    lines = [evaluate.HEADER.replace(" " * 24, f"{'arm':<24}", 1), rule]
    for name in names:
        lines.append(evaluate.line(name, evaluate.total(scores[name])))
        if holdout:
            tuning = {store.document_group(d) for d in scores[name]} - holdout
            lines += [evaluate.line("  tuning", evaluate.total(scores[name], tuning)),
                      evaluate.line("  holdout", evaluate.total(scores[name], holdout))]
    lines += ["", "per document (bounds / lith m / median position error m):", ""]
    width = max(len(n) for n in names) + 2
    lines.append(f"{'document':<24}" + "".join(f"{n[:22]:>{max(width, 24)}}" for n in names))
    for doc_id in sorted(reference_docs(reference)):
        cells = []
        for name in names:
            s = scores[name].get(doc_id, evaluate.Score())
            med = f"{statistics.median(s.position_errors_m):.0f}" if s.position_errors_m else "–"
            cells.append(f"{evaluate.pct(s.boundaries_matched, s.boundaries).strip()} / "
                         f"{evaluate.pct(s.metres_same, s.metres).strip()} / {med}")
        mark = " (holdout)" if store.document_group(doc_id) in holdout else ""
        lines.append(f"{(doc_id + mark)[:24]:<24}" + "".join(f"{c:>{max(width, 24)}}" for c in cells))
    return "\n".join(lines + [""] + evaluate.LEGEND)


def reference_docs(reference: dict[str, dict]) -> set[str]:
    return {r["doc_id"] for r in reference.values()}
