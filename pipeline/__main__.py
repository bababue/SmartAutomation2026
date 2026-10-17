"""Command line entry point.

    python -m pipeline ingest                 # render all pages (no API calls)
    python -m pipeline estimate               # page counts and rough API cost
    python -m pipeline run --doc AUS01        # classify + extract + store selected documents
    python -m pipeline run --all --yes        # everything
    python -m pipeline rebuild                # re-assemble from cached extractions (no API calls)
    python -m pipeline rebuild --doc SEA01 --max-pages 6   # same page limit as the run
    python -m pipeline eval snapshot          # approved records become the evaluation reference
    python -m pipeline eval                   # model output (before corrections) against that reference
    python -m pipeline eval --corrected       # final values, reviewer corrections included
"""

from __future__ import annotations

import argparse
from pathlib import Path
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, corrections, evaluate, ingest, knowledge, llm, store
from .assemble import LOG_KINDS, PageResult, assemble, norm_name

EXTRACT_KINDS = LOG_KINDS | {"coordinate_table"}

# Rough per-page API cost in USD (Opus 5.5 list price). `estimate` also shows the cost measured on cached pages.
COST_CLASSIFY, COST_EXTRACT = 0.008, 0.15


def _select(args) -> list[ingest.SourceDoc]:
    docs = ingest.discover()
    if getattr(args, "doc", None):
        wanted = set(args.doc)
        docs = [d for d in docs if d.doc_id in wanted or d.doc_id.split("__")[0] in wanted]
    return docs


def _ingest(docs, con) -> dict[str, list[ingest.PageImage]]:
    rendered = {}
    for d in docs:
        pages = ingest.render(d)
        rendered[d.doc_id] = pages
        existing = {r["page"]: r for r in store.page_rows(con, d.doc_id)}
        store.save_document(
            con, d.doc_id, d.path, d.kind,
            [
                {
                    "doc_id": p.doc_id, "page": p.page, "image": p.image, "thumb": p.thumb,
                    "width": p.width, "height": p.height,
                    "kind": existing.get(p.page, {}).get("kind"),
                    "language": existing.get(p.page, {}).get("language"),
                    "logs": existing.get(p.page, {}).get("logs"),
                }
                for p in pages
            ],
        )
        print(f"  {d.doc_id}: {len(pages)} pages", flush=True)
    return rendered


def _parallel(fn, items, label):
    results, failed = {}, []
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for i, fut in enumerate(as_completed(futures), 1):
            item = futures[fut]
            try:
                results[(item.doc_id, item.page)] = fut.result()
            except Exception as exc:  # keep going; report at the end
                failed.append((item, exc))
            if i % 10 == 0 or i == len(items):
                print(f"  {label}: {i}/{len(items)}", flush=True)
    for item, exc in failed:
        print(f"  ! {label} failed {item.doc_id} p{item.page}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return results


# Continuation sheets can be separated by a photo or legend page.
CHAIN_GAP = 3


def _chains(pages: list[ingest.PageImage], classes) -> list[list[ingest.PageImage]]:
    """Extraction order. A log page that shares a hole name with the previous log page of the same
    document (per the classification) continues it: it waits for that page and gets its summary as
    context. Independent chains run in parallel."""
    chains: list[list[ingest.PageImage]] = []
    last: dict[str, tuple[ingest.PageImage, set[str], int]] = {}
    for p in sorted(pages, key=lambda p: (p.doc_id, p.page)):
        c = classes[(p.doc_id, p.page)]
        if c.kind.value not in LOG_KINDS:
            chains.append([p])
            continue
        names = {norm_name(log.name) for log in c.logs}
        prev = last.get(p.doc_id)
        if prev and names & prev[1] and p.page - prev[0].page <= CHAIN_GAP:
            index = prev[2]
            chains[index].append(p)
        else:
            index = len(chains)
            chains.append([p])
        last[p.doc_id] = (p, names, index)
    return chains


def _walk(chain, classes, extract):
    """Extract one chain in page order; each page gets the summary of the previous one."""
    results, failed, prev = {}, [], None
    for p in chain:
        kind = classes[(p.doc_id, p.page)].kind.value
        try:
            x = extract(p, kind, llm.sheet_context(prev))
        except Exception as exc:  # keep going; report at the end
            failed.append((p, exc))
            x = None
        if x is not None:
            results[(p.doc_id, p.page)] = x
        prev = x
    return results, failed


def _extract(todo, classes, con=None):
    chains = _chains(todo, classes)
    hints = {doc_id: knowledge.prompt_context(con, doc_id) for doc_id in {p.doc_id for p in todo}} if con else {}
    extract = lambda p, kind, context: llm.extract_page(p, kind, context, hints.get(p.doc_id, ""))
    results, failed, done = {}, [], 0
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        for fut in as_completed([pool.submit(_walk, ch, classes, extract) for ch in chains]):
            r, f = fut.result()
            results.update(r)
            failed += f
            before, done = done, done + len(r) + len(f)
            if done // 10 > before // 10 or done == len(todo):
                print(f"  extract: {done}/{len(todo)}", flush=True)
    for item, exc in failed:
        print(f"  ! extract failed {item.doc_id} p{item.page}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return results


def cmd_ingest(args) -> None:
    con = store.connect()
    _ingest(_select(args), con)


def cmd_estimate(args) -> None:
    con = store.connect()
    rendered = _ingest(_select(args), con)
    n = sum(len(p) for p in rendered.values())
    print(f"{len(rendered)} documents, {n} pages")
    print(f"classification ≈ ${n * COST_CLASSIFY:.0f}; extraction of ~40% log pages ≈ ${n * 0.4 * COST_EXTRACT:.0f}")
    for stage, (pages, usd) in llm.measured_costs().items():
        print(f"measured {stage}: {pages} pages, ${usd:.2f} (${usd / pages:.4f} per page)")


def cmd_run(args) -> None:
    if not args.doc and not args.all:
        sys.exit("choose documents with --doc NAME (repeatable) or --all")
    if config.LLM_BACKEND == "api" and not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("No Anthropic credentials: put ANTHROPIC_API_KEY=... into .env (git-ignored), "
                 "or set LLM_BACKEND=claude-code to use the local Claude Code login.")
    con = store.connect()
    docs = _select(args)
    print(f"Ingest {len(docs)} documents (LLM backend: {config.LLM_BACKEND})")
    rendered = _ingest(docs, con)
    if args.max_pages:  # treat the first N pages as the whole document for this run
        rendered = {doc_id: ps[: args.max_pages] for doc_id, ps in rendered.items()}
    pages = [p for ps in rendered.values() for p in ps]
    est = len(pages) * COST_CLASSIFY + len(pages) * 0.4 * COST_EXTRACT
    if est > 5 and not args.yes:
        sys.exit(f"{len(pages)} pages, estimated ≈ ${est:.0f}. Re-run with --yes to proceed.")

    print(f"Classify {len(pages)} pages")
    classes = _parallel(llm.classify_page, pages, "classify")
    for (doc_id, page_no), c in classes.items():
        con.execute(
            "UPDATE pages SET kind = ?, language = ?, logs = ? WHERE doc_id = ? AND page = ?",
            (c.kind.value, c.language, store.as_json([log.model_dump() for log in c.logs]), doc_id, page_no),
        )
    con.commit()

    todo = [p for p in pages if (c := classes.get((p.doc_id, p.page))) and c.kind.value in EXTRACT_KINDS]
    print(f"Extract {len(todo)} log/coordinate pages")
    extractions = _extract(todo, classes, con)
    lab_pages = [p for p in pages if (c := classes.get((p.doc_id, p.page))) and c.kind.value == "lab_results"]
    if lab_pages:
        print(f"Extract {len(lab_pages)} lab result pages")
    labs = _parallel(llm.extract_lab_results, lab_pages, "lab results")
    _store(con, docs, rendered, classes, extractions, labs)


def _store(con, docs, rendered, classes, extractions, labs=None, dry_run: bool = False) -> None:
    """Lab sheets are optional: a failed lab page does not hold back the document's logs.
    dry_run: assemble and report, but write nothing."""
    labs = labs or {}
    source_file = {d.doc_id: d.path for d in docs}

    def complete(doc_id: str) -> bool:
        pages = rendered[doc_id]
        return all((p.doc_id, p.page) in classes for p in pages) and all(
            (p.doc_id, p.page) in extractions
            for p in pages
            if classes[(p.doc_id, p.page)].kind.value in EXTRACT_KINDS
        )

    done = [doc_id for doc_id in rendered if complete(doc_id)]
    if dry_run:  # nothing is written, so show what is there so far
        done = [doc_id for doc_id in rendered if all((p.doc_id, p.page) in classes for p in rendered[doc_id])]
    skipped = sorted(set(rendered) - set(done))
    partial = sorted(d for d in done if not complete(d))
    if partial:
        print(f"  dry run includes documents still being extracted: {', '.join(partial)}")
    if skipped:
        print(f"  ! kept previous results for incomplete documents: {', '.join(skipped)}", file=sys.stderr)
    results = [
        PageResult(p.doc_id, source_file[p.doc_id], p.page, classes[(p.doc_id, p.page)].kind.value,
                   extractions.get((p.doc_id, p.page)), p.text, classes[(p.doc_id, p.page)].crs_text,
                   labs.get((p.doc_id, p.page)))
        for doc_id in done for p in rendered[doc_id]
    ]
    boreholes = assemble(results)
    extracted = {bh.borehole_id: store.extracted_summary(bh) for bh in boreholes}  # before fixes, for `eval`
    # Per document group: read knowledge and corrections and replace the group's records in one short write
    # transaction. A correction saved meanwhile cannot be lost, and reviewers wait for one group at most.
    groups: dict[str, list] = {}
    for doc_id in done:
        groups.setdefault(doc_id.split("__")[0], [])
    for bh in boreholes:
        groups.setdefault(store.document_group(bh.borehole_id), []).append(bh)
    for group, records in groups.items():
        con.execute("BEGIN IMMEDIATE")
        try:
            knowledge.apply_all(con, records)  # what reviewers taught beyond single records
            for bh in records:  # reviewer fixes survive re-extraction, and win over learned knowledge
                corrections.apply(bh, store.corrections_for(con, bh.borehole_id, store.uid_for(con, bh)))
            if dry_run:
                con.rollback()
                continue
            previous: dict[str, dict] = {}  # human decisions carry over if the content is unchanged
            for doc_id in done:
                if doc_id.split("__")[0] == group:
                    previous.update(store.delete_document_boreholes(con, doc_id))
            for bh in records:
                store.save_borehole(con, bh, commit=False, previous=previous.get(bh.borehole_id),
                                    extracted=extracted[bh.borehole_id])
            con.commit()
        except BaseException:
            con.rollback()
            raise
    if dry_run:
        _report(boreholes, len(done))
        return
    orphans = store.orphan_corrections(con)
    if orphans:
        print(f"  ! {len(orphans)} reviewer corrections belong to holes no current record carries "
              f"(e.g. {orphans[0]['target']}); see /api/corrections/orphans", file=sys.stderr)
    located = sum(1 for b in boreholes if b.location.lat is not None)
    auto = sum(1 for b in boreholes if b.review_status.value == "auto_approved")
    print(f"Stored {len(boreholes)} boreholes ({located} georeferenced) from {len(done)} documents in {config.DB_PATH}")
    print(f"  released automatically: {auto}, waiting for review: {len(boreholes) - auto} "
          f"(threshold {round(config.AUTO_APPROVE_THRESHOLD * 100)})")


def _report(boreholes, n_docs: int) -> None:
    """One line per record: what a run would store (for checking results while another run writes)."""
    for bh in sorted(boreholes, key=lambda b: b.borehole_id):
        findings = [i for i in bh.issues if not i.startswith("unreadable")]
        print(f"  {bh.borehole_id:<28} {bh.review_status.value:<15} score {round((bh.score or 0) * 100):>3}  "
              f"{len(bh.layers):>2} layers  {len(bh.lab_results):>3} lab  | {'; '.join(findings[:3])[:150]}")
    auto = sum(1 for b in boreholes if b.review_status.value == "auto_approved")
    print(f"Dry run: {len(boreholes)} boreholes from {n_docs} documents, {auto} would be released automatically; "
          "nothing written")


def cmd_rebuild(args) -> None:
    """Re-run assembly and georeferencing from cached LLM output only."""
    rebuild(_select(args), args.max_pages, args.dry_run)


def rebuild(docs, max_pages: int | None = None, dry_run: bool = False) -> None:
    """Assemble, apply knowledge and corrections, and store, from the cache only (no API calls)."""
    con = store.connect()
    rendered = {d.doc_id: ingest.render(d) for d in docs}
    if max_pages:  # same page limit as the run that filled the cache
        rendered = {doc_id: ps[: max_pages] for doc_id, ps in rendered.items()}
    classes, extractions = {}, {}
    for ps in rendered.values():
        for p in ps:
            c = llm.cached_classification(p)
            if c is not None:
                classes[(p.doc_id, p.page)] = c
    todo = [p for ps in rendered.values() for p in ps
            if (c := classes.get((p.doc_id, p.page))) and c.kind.value in EXTRACT_KINDS]
    for chain in _chains(todo, classes):  # same chains and context as the run, so the cache keys match
        extractions.update(_walk(chain, classes, llm.cached_extraction)[0])
    labs = {(p.doc_id, p.page): x for ps in rendered.values() for p in ps
            if (c := classes.get((p.doc_id, p.page))) and c.kind.value == "lab_results"
            and (x := llm.cached_lab_results(p)) is not None}
    print(f"{len(classes)} classified pages, {len(extractions)} extracted pages from cache")
    _store(con, docs, rendered, classes, extractions, labs, dry_run=dry_run)


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
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                            cwd=config.ROOT).stdout.strip() or "unknown"
    (out / "README.md").write_text(SHARE_README.format(
        commit=commit, created=datetime.date.today().isoformat(), source=config.DB_PATH.name,
        boreholes=counts[0], documents=counts[1]))
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
    p.add_argument("action", nargs="?", choices=["compare", "snapshot"], default="compare")
    p.add_argument("--corrected", action="store_true", help="compare final values instead of the model output")
    p.set_defaults(fn=evaluate.cmd_eval)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
