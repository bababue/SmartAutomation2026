"""Prompt experiments: try an extraction prompt on the reference documents without touching the main cache,
the main database or the reviewers' work.

    python -m pipeline experiment baseline          # the main cache into a fresh DB (free)
    python -m pipeline experiment init NAME         # data/eval/experiments/NAME/prompt.md = the current prompt
    python -m pipeline experiment run NAME --yes    # extract the reference documents with NAME's prompt
    python -m pipeline experiment compare           # every arm against the reference, per document

Without a reference, arms run with `--doc` and are compared on signals that need none (text-layer
confirmation, blocking findings, releases); see `proxy`.

Each arm has its own extraction cache and a fresh database without corrections or knowledge, so the scores
are the model's alone. Classifications and lab sheets come from the main cache: a prompt experiment changes
extraction only. Model and effort come from the usual settings (EXTRACT_MODEL, EXTRACT_EFFORT) and are part
of the cache key. The main cache can hold pages first extracted with reviewer knowledge in the prompt, so
the baseline may be slightly favoured on documents that have knowledge entries.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import statistics
from pathlib import Path

from schema.borehole import Borehole

from . import config, evaluate, llm, scoring, store

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
    path.write_text(llm.EXTRACT_SYSTEM, encoding="utf-8")
    return path


def use_prompt(name: str) -> str:
    """Point this process's extraction at the arm's prompt and cache. The prompt's hash replaces the prompt
    version in the extraction key, so an edited prompt.md never reuses stale pages."""
    path = arm_dir(name) / "prompt.md"
    if not path.exists():
        raise SystemExit(f"no prompt for {name}: run `python -m pipeline experiment init {name}` and edit {path}")
    prompt = path.read_text(encoding="utf-8")
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
    store.migrate(path)
    return store.connect(path)


def proxy(con: sqlite3.Connection, docs, rendered) -> dict:
    """Signals that need no reference, for comparing arms on documents nobody has checked yet: how many
    layer boundaries the PDF text layer confirms (digital PDFs only), blocking findings and releases.
    Hints only: a text layer can be incomplete, and fewer findings can also mean fewer layers."""
    texts = {(d.path, p.page): p.text for d in docs for p in rendered[d.doc_id]}
    found = checked = released = 0
    blocking: dict[str, int] = {}
    records = [Borehole.model_validate_json(r["data"]) for r in con.execute("SELECT data FROM boreholes")]
    for bh in records:
        f, t, _, _ = scoring.text_check(bh, texts)
        found, checked = found + f, checked + t
        released += bh.review_status.value == "auto_approved"
        for issue in bh.issues:
            if scoring.finding_severity(issue) == "blocking":
                code = scoring.finding_code(issue)
                blocking[code] = blocking.get(code, 0) + 1
    return {"documents": sorted(d.doc_id for d in docs), "records": len(records),
            "layers": sum(len(bh.layers) for bh in records), "released": released,
            "text_found": found, "text_checked": checked, "blocking": blocking}


def save_proxy(name: str, data: dict) -> None:
    (arm_dir(name) / "proxy.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


def proxy_report(names: list[str]) -> list[str]:
    data = {n: json.loads(p.read_text(encoding="utf-8")) for n in names if (p := arm_dir(n) / "proxy.json").exists()}
    if not data:
        return []
    codes = sorted({c for d in data.values() for c in d["blocking"]})
    lines = ["without reference (hints only):", "",
             f"{'arm':<24} {'docs':>4} {'records':>7} {'layers':>6} {'released':>8} {'text ok':>8} {'blocking':>8}  by code"]
    for name, d in data.items():
        by_code = ", ".join(f"{c} {d['blocking'][c]}" for c in codes if d["blocking"].get(c))
        lines.append(f"{name[:24]:<24} {len(d['documents']):>4} {d['records']:>7} {d['layers']:>6} {d['released']:>8} "
                     f"{evaluate.pct(d['text_found'], d['text_checked']):>8} {sum(d['blocking'].values()):>8}  {by_code}")
    if len({tuple(d["documents"]) for d in data.values()}) > 1:
        lines.append("! the arms ran on different documents: run each with the same --doc options")
    return lines + ["text ok: layer boundaries found literally in the PDF text layer (digital PDFs only).", ""]


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


def arm_reference(name: str) -> dict[str, dict]:
    """An arm's records in reference form, to measure agreement with it where nobody has checked the
    documents yet. Hints only: agreeing with another model is not being right."""
    path = arm_dir(name) / "boreholes.sqlite"
    if not path.exists():
        raise SystemExit(f"no results for arm {name!r}")
    con = store.connect(path)
    try:
        return evaluate.current_entries(con)[0]
    finally:
        con.close()


def compare_arms(against: str | None = None) -> str:
    names = arms()
    if not names:
        return "no experiment results yet: run `python -m pipeline experiment baseline` first"
    head = proxy_report(names)
    reference = arm_reference(against) if against else evaluate.load_reference()
    if not reference and against:
        return "\n".join(head + [f"arm {against!r} has no records"])
    if not reference:
        return "\n".join(head + ["no reference yet: approve records in the review UI, then run "
                                 "`python -m pipeline eval snapshot`"])
    holdout = evaluate.load_holdout()
    scores = {name: arm_scores(name, reference) for name in names}
    rule = "-" * len(evaluate.HEADER)
    title = f"agreement with arm {against!r} (hints only, not accuracy):" if against else "against the reference:"
    lines = head + [title, "", evaluate.HEADER.replace(" " * 24, f"{'arm':<24}", 1), rule]
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


def reference_pages(con: sqlite3.Connection, reference: dict[str, dict]) -> set[tuple[str, int]]:
    """(file, page) of the log pages the reference records were read from, per the main database."""
    ids = list(reference)
    return {(r[0], r[1]) for i in range(0, len(ids), 500) for r in con.execute(
        "SELECT DISTINCT s.file, s.page FROM record_sources s JOIN boreholes b ON b.rid = s.rid "
        f"WHERE s.role = 'log' AND b.id IN ({','.join('?' * len(ids[i:i + 500]))})", ids[i:i + 500])}


def reference_docs(reference: dict[str, dict]) -> set[str]:
    return {r["doc_id"] for r in reference.values()}
