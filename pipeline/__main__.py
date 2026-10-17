"""Command line entry point.

    python -m pipeline ingest                 # render all pages (no API calls)
    python -m pipeline estimate               # page counts and rough API cost
    python -m pipeline run --doc site-report  # classify + extract + store selected documents
    python -m pipeline run --all --yes        # everything
    python -m pipeline rebuild                # re-assemble from cached extractions (no API calls)
    python -m pipeline rebuild --doc site-report --max-pages 6   # same page limit as the run
    python -m pipeline share --out ~/package  # copy of the database and pages for colleagues
    python -m pipeline eval snapshot          # approved records become the evaluation reference
    python -m pipeline eval                   # model output (before corrections) against that reference
    python -m pipeline eval --corrected       # final values, reviewer corrections included
    python -m pipeline eval holdout --doc GROUP  # keep document groups out of prompt tuning
    python -m pipeline eval audit             # sample automatic releases: how often are they wrong?
    python -m pipeline experiment baseline    # the main cache's extractions as an arm (no API calls)
    python -m pipeline experiment init NAME   # a prompt variant to edit (see experiment.py)
    python -m pipeline experiment run NAME    # that variant on the reference documents
    python -m pipeline experiment compare     # all arms against the reference
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, evaluate, experiment, llm, runner
from .runner import COST_CLASSIFY, COST_EXTRACT, page_cost


def cmd_ingest(args) -> None:
    runner.ingest_docs(runner.select(args.doc))


def cmd_estimate(args) -> None:
    rendered = runner.ingest_docs(runner.select(args.doc))
    n = sum(len(p) for p in rendered.values())
    print(f"{len(rendered)} documents, {n} pages")
    print(f"classification with {config.CLASSIFY_MODEL} ≈ ${n * page_cost(COST_CLASSIFY, config.CLASSIFY_MODEL):.2f}; "
          f"extraction of ~40% log pages with {config.EXTRACT_MODEL} ≈ "
          f"${n * 0.4 * page_cost(COST_EXTRACT, config.EXTRACT_MODEL):.2f}")
    for stage, (pages, usd) in llm.measured_costs().items():
        print(f"measured {stage}: {pages} pages, ${usd:.2f} (${usd / pages:.4f} per page)")


def cmd_run(args) -> None:
    if not args.doc and not args.all:
        sys.exit("choose documents with --doc NAME (repeatable) or --all")
    try:
        runner.run(runner.select(args.doc), args.max_pages, args.yes)
    except RuntimeError as exc:
        sys.exit(str(exc))


def cmd_rebuild(args) -> None:
    runner.rebuild(runner.select(args.doc), args.max_pages, args.dry_run)


def cmd_experiment(args) -> None:
    if args.action in ("init", "run") and args.name in (None, experiment.BASELINE):
        sys.exit(f"name the experiment: python -m pipeline experiment {args.action} NAME (not '{experiment.BASELINE}')")
    if args.action == "init":
        print(f"edit {experiment.init(args.name)}, then run `python -m pipeline experiment run {args.name}`")
        return
    if args.action == "compare":
        print(experiment.compare_arms(against=args.against))
        return
    try:
        runner.run_experiment(args.action, args.name, args.doc, args.yes, args.reference_pages)
    except RuntimeError as exc:
        sys.exit(str(exc))
    print()
    print(experiment.compare_arms())


SHARE_README = """# Borehole Atlas data package

Confidential: real project data from the hackathon challenge. Share only within the team, never via
cloud services, and delete it after the event (including copies).

Contents
- boreholes.sqlite   database (records, review decisions, corrections, knowledge)
- pages/             rendered source pages shown next to each record (optional)
- cache/             cached model output, needed only for `pipeline rebuild` (optional)

Run it
1. Get the code: the repository at commit {commit} (branch marvin) and set it up as in its README
   (python3 -m venv .venv; .venv/bin/pip install -r requirements.txt; npm --prefix web install).
2. Copy the package into the repository's data/ folder:
   boreholes.sqlite -> data/boreholes.sqlite, pages/ -> data/pages/, cache/ -> data/cache/
   (or point DB_PATH / DATA_DIR at the package folder).
3. Start: .venv/bin/uvicorn api.main:app --port 8000   and   npm --prefix web run dev
   then open http://localhost:5173. Without pages/ the map, profiles and exports work; the source page
   next to a record stays empty.

Created {created} from {source}: {boreholes} boreholes, {documents} documents.
"""


def cmd_share(args) -> None:
    """A consistent copy of the database (SQLite backup API, safe while the API runs), the page images and
    optionally the cache, with a README for colleagues."""
    import datetime
    import shutil
    import sqlite3
    import subprocess

    out = Path(args.out).expanduser()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(out / "boreholes.sqlite")
    with dst:
        src.backup(dst)
    counts = dst.execute("SELECT (SELECT COUNT(*) FROM boreholes), (SELECT COUNT(*) FROM documents)").fetchone()
    dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm to forget
    dst.close()
    src.close()
    if not args.no_pages:
        shutil.copytree(config.PAGES_DIR, out / "pages")
    if args.with_cache:
        shutil.copytree(config.CACHE_DIR, out / "cache")
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                cwd=config.ROOT).stdout.strip() or "unknown"
    except OSError:  # no git installed, e.g. in the Docker image
        commit = "unknown"
    (out / "README.md").write_text(SHARE_README.format(
        commit=commit, created=datetime.date.today().isoformat(), source=config.DB_PATH.name,
        boreholes=counts[0], documents=counts[1]), encoding="utf-8")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    print(f"Package in {out}: {counts[0]} boreholes, {size:.0f} MB. Confidential: share locally only "
          "(AirDrop, USB, scp), not via cloud services.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in [("ingest", cmd_ingest), ("estimate", cmd_estimate), ("run", cmd_run), ("rebuild", cmd_rebuild)]:
        p = sub.add_parser(name)
        p.add_argument("--doc", action="append", help="document id or folder prefix (repeatable)")
        p.set_defaults(fn=fn)
        if name == "run":
            p.add_argument("--all", action="store_true")
            p.add_argument("--yes", action="store_true", help="skip the cost confirmation")
        if name in ("run", "rebuild"):
            p.add_argument("--max-pages", type=int, help="only the first N pages of each document")
        if name == "rebuild":
            p.add_argument("--dry-run", action="store_true", help="assemble and report, write nothing")
    p = sub.add_parser("share", help="package the database (and page images) for colleagues")
    p.add_argument("--out", required=True, help="empty or new folder for the package")
    p.add_argument("--no-pages", action="store_true", help="leave out the page images (much smaller)")
    p.add_argument("--with-cache", action="store_true", help="include the model cache, for rebuilds")
    p.set_defaults(fn=cmd_share)
    p = sub.add_parser("eval", help="extraction quality against human-checked records")
    p.add_argument("action", nargs="?", choices=["compare", "snapshot", "holdout", "audit"], default="compare")
    p.add_argument("--corrected", action="store_true", help="compare final values instead of the model output")
    p.set_defaults(fn=evaluate.cmd_eval)
    p.add_argument("--doc", action="append", help="holdout: document group (repeatable; none clears it)")
    p.add_argument("--size", type=int, default=15, help="audit: records to draw")
    p.add_argument("--seed", type=int, default=2026, help="audit: random seed, so the sample can be redrawn")
    p.add_argument("--redraw", action="store_true", help="audit: replace the current sample")
    p = sub.add_parser("experiment", help="try an extraction prompt on the reference documents, isolated")
    p.add_argument("action", choices=["baseline", "init", "run", "compare"])
    p.add_argument("name", nargs="?")
    p.add_argument("--doc", action="append", help="documents to use instead of the reference documents")
    p.add_argument("--yes", action="store_true", help="confirm the extraction cost")
    p.add_argument("--reference-pages", action="store_true",
                   help="run: re-extract only the pages of reference records; other pages come from the main cache")
    p.add_argument("--against", metavar="ARM", help="compare: agreement with this arm instead of the reference")
    p.set_defaults(fn=cmd_experiment)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
