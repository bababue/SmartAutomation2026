"""Command line entry point.

    python -m pipeline ingest                 # render all pages (no API calls)
    python -m pipeline estimate               # page counts and rough API cost
    python -m pipeline run --doc AUS01        # classify + extract + store selected documents
    python -m pipeline run --all --yes        # everything
    python -m pipeline rebuild                # re-assemble from cached extractions (no API calls)
    python -m pipeline rebuild --doc SEA01 --max-pages 6   # same page limit as the run
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, ingest, llm, store
from .assemble import LOG_KINDS, PageResult, assemble

EXTRACT_KINDS = LOG_KINDS | {"coordinate_table"}

# Rough per-page API cost in USD (Opus 5.5 list price, measured once a key is available).
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


def cmd_ingest(args) -> None:
    con = store.connect()
    _ingest(_select(args), con)


def cmd_estimate(args) -> None:
    con = store.connect()
    rendered = _ingest(_select(args), con)
    n = sum(len(p) for p in rendered.values())
    print(f"{len(rendered)} documents, {n} pages")
    print(f"classification ≈ ${n * COST_CLASSIFY:.0f}; extraction of ~40% log pages ≈ ${n * 0.4 * COST_EXTRACT:.0f}")


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
    extractions = _parallel(lambda p: llm.extract_page(p, classes[(p.doc_id, p.page)].kind.value), todo, "extract")
    _store(con, docs, rendered, classes, extractions)


def _store(con, docs, rendered, classes, extractions) -> None:
    source_file = {d.doc_id: d.path for d in docs}

    def complete(doc_id: str) -> bool:
        pages = rendered[doc_id]
        return all((p.doc_id, p.page) in classes for p in pages) and all(
            (p.doc_id, p.page) in extractions
            for p in pages
            if classes[(p.doc_id, p.page)].kind.value in EXTRACT_KINDS
        )

    done = [doc_id for doc_id in rendered if complete(doc_id)]
    skipped = sorted(set(rendered) - set(done))
    if skipped:
        print(f"  ! kept previous results for incomplete documents: {', '.join(skipped)}", file=sys.stderr)
    results = [
        PageResult(p.doc_id, source_file[p.doc_id], p.page, classes[(p.doc_id, p.page)].kind.value,
                   extractions.get((p.doc_id, p.page)), p.text)
        for doc_id in done for p in rendered[doc_id]
    ]
    boreholes = assemble(results)
    with con:  # replace complete documents atomically; human decisions carry over if content is unchanged
        previous: dict[str, dict] = {}
        for doc_id in done:
            previous.update(store.delete_document_boreholes(con, doc_id))
        for bh in boreholes:
            store.save_borehole(con, bh, commit=False, previous=previous.get(bh.borehole_id))
    located = sum(1 for b in boreholes if b.location.lat is not None)
    auto = sum(1 for b in boreholes if b.review_status.value == "auto_approved")
    print(f"Stored {len(boreholes)} boreholes ({located} georeferenced) from {len(done)} documents in {config.DB_PATH}")
    print(f"  released automatically: {auto}, waiting for review: {len(boreholes) - auto} "
          f"(threshold {round(config.AUTO_APPROVE_THRESHOLD * 100)})")


def cmd_rebuild(args) -> None:
    """Re-run assembly and georeferencing from cached LLM output only."""
    con = store.connect()
    docs = _select(args)
    rendered = {d.doc_id: ingest.render(d) for d in docs}
    if args.max_pages:  # same page limit as the run that filled the cache
        rendered = {doc_id: ps[: args.max_pages] for doc_id, ps in rendered.items()}
    classes, extractions = {}, {}
    for ps in rendered.values():
        for p in ps:
            c = llm.cached_classification(p)
            if c is None:
                continue
            classes[(p.doc_id, p.page)] = c
            x = llm.cached_extraction(p, c.kind.value)
            if x is not None:
                extractions[(p.doc_id, p.page)] = x
    print(f"{len(classes)} classified pages, {len(extractions)} extracted pages from cache")
    _store(con, docs, rendered, classes, extractions)


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
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
