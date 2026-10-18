"""Pipeline runs: ingest, classify, extract and store documents, or rebuild them from the cache."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import urllib.parse
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TypeVar

from schema.borehole import Borehole

from . import config, coordcheck, corrections, evaluate, experiment, ingest, knowledge, llm, store
from .assemble import LOG_KINDS, PageResult, assemble, flag_position_outliers, placeholder, unstack_place_positions
from .dedupe import dedupe
from .extraction_models import CoordinateReading, PageClassification, PageExtraction, PageLabResults
from .identity import document_id, log_key, norm_name

EXTRACT_KINDS = LOG_KINDS | {"coordinate_table"}

Rendered = dict[str, list[ingest.PageImage]]  # by doc_id
Classes = dict[tuple[str, int], PageClassification]  # by (doc_id, page)
Extractions = dict[tuple[str, int], PageExtraction]
T = TypeVar("T")

# Rough per-page API cost in USD at the Opus 5.5 list price; `estimate` also shows the cost measured on cached pages.
COST_CLASSIFY, COST_EXTRACT = 0.008, 0.15


def page_cost(opus_cost: float, model: str) -> float:
    """Scaled by the model's list price; models outside PRICES (other providers) are estimated at the Opus price."""
    opus = llm.PRICES["claude-opus-5-5"]
    price = llm.PRICES.get(model.removeprefix("anthropic/").replace(".", "-"), opus)
    return opus_cost * price[1] / opus[1]


def select(wanted: list[str] | None) -> list[ingest.SourceDoc]:
    """All documents, or those whose id or document group is in `wanted`."""
    docs = ingest.discover()
    if wanted:
        docs = [d for d in docs if d.doc_id in wanted or store.document_group(d.doc_id) in wanted]
    return docs


def missing_credentials() -> str | None:
    if config.LLM_BACKEND == "api" and not os.getenv("ANTHROPIC_API_KEY"):
        return ("No Anthropic credentials: put ANTHROPIC_API_KEY=... into .env (git-ignored), "
                "or set LLM_BACKEND=claude-code to use the local Claude Code login.")
    local = urllib.parse.urlsplit(config.OPENAI_BASE_URL).hostname in ("localhost", "127.0.0.1")
    if config.LLM_BACKEND == "openai" and not local and not os.getenv("OPENAI_API_KEY"):
        return f"No key for {config.OPENAI_BASE_URL}: put OPENAI_API_KEY=... into .env (git-ignored)."
    return None


def _ingest(docs: list[ingest.SourceDoc], con: sqlite3.Connection, dataset_dir: Path | None = None) -> Rendered:
    rendered = {}
    for d in docs:
        pages = ingest.render(d, dataset_dir)
        rendered[d.doc_id] = pages
        store.save_document(con, d.doc_id, d.path, [(p.page, p.image) for p in pages])
        print(f"  {d.doc_id}: {len(pages)} pages", flush=True)
    return rendered


def ingest_docs(docs: list[ingest.SourceDoc]) -> Rendered:
    store.migrate()
    con = store.connect()
    try:
        return _ingest(docs, con)
    finally:
        con.close()


# stage, items done, items in the stage
Progress = Callable[[str, int, int], None]


def _parallel(fn: Callable[[ingest.PageImage], T], items: list[ingest.PageImage], label: str,
              progress: Progress | None = None) -> dict[tuple[str, int], T]:
    results, failed = {}, []
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for i, fut in enumerate(as_completed(futures), 1):
            item = futures[fut]
            try:
                results[(item.doc_id, item.page)] = fut.result()
            except Exception as exc:  # keep going; report at the end
                failed.append((item, exc))
            if progress:
                progress(label, i, len(items))
            if i % 10 == 0 or i == len(items):
                print(f"  {label}: {i}/{len(items)}", flush=True)
    for item, exc in failed:
        print(f"  ! {label} failed {item.doc_id} p{item.page}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return results


# Continuation sheets can be separated by a photo or legend page.
CHAIN_GAP = 3


def _chains(pages: list[ingest.PageImage], classes: Classes) -> list[list[ingest.PageImage]]:
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


def expected_logs(c: PageClassification) -> int:
    """Logs a page must return, as the classification counted them. Probe records (dynamic probe, CPT) are left
    out: they often carry blow counts only."""
    return max(len(c.logs), 1) if c.kind.value in LOG_KINDS - {"probe_log"} else 0


def _walk(chain: list[ingest.PageImage], classes: Classes,
          extract: Callable[[ingest.PageImage, str, str, int], PageExtraction | None]
          ) -> tuple[Extractions, list[tuple[ingest.PageImage, Exception]]]:
    """Extract one chain in page order; each page gets the summary of the previous one."""
    results, failed, prev = {}, [], None
    for p in chain:
        c = classes[(p.doc_id, p.page)]
        kind = c.kind.value
        try:
            x = extract(p, kind, llm.sheet_context(prev), expected_logs(c))
        except Exception as exc:  # keep going; report at the end
            failed.append((p, exc))
            x = None
        if x is not None:
            results[(p.doc_id, p.page)] = x
        prev = x
    return results, failed


def _cached_extractions(chains: list[list[ingest.PageImage]], classes: Classes) -> Extractions:
    """Cached extractions, walked in the run's chains so each page gets the same context."""
    extractions = {}
    for chain in chains:
        extractions.update(_walk(chain, classes, llm.cached_extraction)[0])
    return extractions


def _extract(todo: list[ingest.PageImage], classes: Classes, con: sqlite3.Connection | None = None, retry: bool = True,
             progress: Progress | None = None) -> tuple[Extractions, int]:
    """retry: read a page that came back with fewer logs than classified once more (a model call)."""
    chains = _chains(todo, classes)
    hints = {doc_id: knowledge.prompt_context(con, doc_id) for doc_id in {p.doc_id for p in todo}} if con else {}
    extract = lambda p, kind, context, expected: llm.extract_page(p, kind, context, hints.get(p.doc_id, ""),
                                                                  expected, retry)
    results, failed, done = {}, [], 0
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        for fut in as_completed([pool.submit(_walk, ch, classes, extract) for ch in chains]):
            r, f = fut.result()
            results.update(r)
            failed += f
            before, done = done, done + len(r) + len(f)
            if progress:
                progress("extract", done, len(todo))
            if done // 10 > before // 10 or done == len(todo):
                print(f"  extract: {done}/{len(todo)}", flush=True)
    for item, exc in failed:
        print(f"  ! extract failed {item.doc_id} p{item.page}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return results, len(failed)


def _uncached(todo: list[ingest.PageImage], classes: Classes, retry: bool) -> list[ingest.PageImage]:
    """Pages whose extraction would call the model: the lookup `_extract` makes, with every call refused. The
    rest of a chain counts from its first such page on, as their context comes from that page's new answer."""
    new = []
    extract = lambda p, kind, context, expected: llm.extract_page(p, kind, context, "", expected, retry)
    with llm.guarded({p.doc_id for p in todo}, llm.CallGuard(max_calls=0)):
        for chain in _chains(todo, classes):
            _, failed = _walk(chain, classes, extract)
            if failed:
                new += chain[chain.index(failed[0][0]):]
    return new


def _check_max_pages(max_pages: int | None) -> None:
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")


def _truncate(rendered: Rendered, max_pages: int | None) -> tuple[Rendered, set[str]]:
    """The first `max_pages` pages of each document, and the documents that lost pages to the cap."""
    if max_pages is None:
        return rendered, set()
    truncated = {doc_id for doc_id, ps in rendered.items() if len(ps) > max_pages}
    return {doc_id: ps[:max_pages] for doc_id, ps in rendered.items()}, truncated


def run(docs: list[ingest.SourceDoc], max_pages: int | None = None, yes: bool = True,
        dataset_dir: Path | None = None, progress: Progress | None = None,
        guard: llm.CallGuard | None = None, job_id: str | None = None) -> None:
    """Ingest, classify, extract and store the given documents (costs API calls). guard: cancels documents
    and limits the calls; every call is recorded in llm_calls (with job_id, for an upload). A document cancelled
    before the store step leaves nothing behind; all of them cancelled raises llm.Cancelled."""
    _check_max_pages(max_pages)
    if msg := missing_credentials():
        raise RuntimeError(msg)
    guard = guard or llm.CallGuard()
    store.migrate()
    con, calls, calls_lock = store.connect(), store.connect(), threading.Lock()

    def record(call: dict) -> None:
        with calls_lock:  # one connection for the worker threads
            store.record_llm_call(calls, {**call, "job_id": job_id})

    guard.record = record
    try:
        with llm.guarded((d.doc_id for d in docs), guard):
            _run(con, docs, max_pages, yes, dataset_dir, progress, guard)
    finally:
        con.close()
        calls.close()


def _check(docs: list[ingest.SourceDoc], guard: llm.CallGuard) -> None:
    if guard.exhausted:
        raise llm.CallLimitReached(f"stopped after {guard.calls} model calls, the limit for this run")
    if all(guard.cancelled(d.doc_id) for d in docs):
        raise llm.Cancelled("cancelled")


def _run(con: sqlite3.Connection, docs: list[ingest.SourceDoc], max_pages: int | None, yes: bool,
         dataset_dir: Path | None, progress: Progress | None, guard: llm.CallGuard) -> None:
    print(f"Ingest {len(docs)} documents (LLM backend: {config.LLM_BACKEND})")
    rendered, truncated = _truncate(_ingest(docs, con, dataset_dir), max_pages)
    pages = [p for ps in rendered.values() for p in ps]
    est = len(pages) * (page_cost(COST_CLASSIFY, config.CLASSIFY_MODEL)
                        + 0.4 * page_cost(COST_EXTRACT, config.EXTRACT_MODEL))
    if est > 5 and not yes:
        raise RuntimeError(f"{len(pages)} pages, estimated ≈ ${est:.2f}. Re-run with --yes to proceed.")

    print(f"Classify {len(pages)} pages")
    if progress:
        progress("classify", 0, len(pages))
    classes = _parallel(llm.classify_page, pages, "classify", progress)
    con.executemany("UPDATE pages SET kind = ? WHERE doc_id = ? AND page = ?",
                    [(c.kind.value, doc_id, page_no) for (doc_id, page_no), c in classes.items()])
    con.commit()
    _check(docs, guard)

    todo = extract_todo(rendered, classes)
    print(f"Extract {len(todo)} log/coordinate pages")
    if progress:
        progress("extract", 0, len(todo))
    extractions, _ = _extract(todo, classes, con, progress=progress)
    lab_pages = [p for p in pages if (c := classes.get((p.doc_id, p.page))) and c.kind.value == "lab_results"]
    if lab_pages:
        print(f"Extract {len(lab_pages)} lab result pages")
    labs = _parallel(llm.extract_lab_results, lab_pages, "lab results", progress)
    _check(docs, guard)
    targets = list(coordcheck.targets(todo, extractions))
    print(f"Read the printed coordinates of {len(targets)} holes a second time")
    if progress:
        progress("coordinates", 0, 0)
    coords = coordcheck.readings(targets, llm.read_coordinates)
    _check(docs, guard)
    dropped = [d.doc_id for d in docs if guard.cancelled(d.doc_id)]
    guard.dropped = set(dropped)  # a cancellation from here on comes too late
    if dropped:
        print(f"  {len(dropped)} documents cancelled, nothing stored for them")
        store.delete_documents(con, dropped)
        docs = [d for d in docs if d.doc_id not in dropped]
        rendered = {doc_id: ps for doc_id, ps in rendered.items() if doc_id not in dropped}
    if progress:
        progress("store", 0, 0)
    published = _store(con, docs, rendered, classes, extractions, labs, incomplete_docs=truncated, coords=coords)
    if set(rendered) != published:
        raise RuntimeError("Some documents were incomplete; their previous records were preserved")


def rebuild(docs: list[ingest.SourceDoc], max_pages: int | None = None, dry_run: bool = False) -> None:
    """Assemble, apply knowledge and corrections, and store, from the cache only (no API calls)."""
    _check_max_pages(max_pages)
    store.migrate()
    con = store.connect()
    try:
        # Registering the documents gives a fresh database its source pages; a dry run writes nothing.
        rendered, truncated = _truncate({d.doc_id: ingest.render(d) for d in docs} if dry_run else _ingest(docs, con),
                                        max_pages)
        classes, labs = cached_inputs(rendered)
        extractions = _cached_extractions(_chains(extract_todo(rendered, classes), classes), classes)
        print(f"{len(classes)} classified pages, {len(extractions)} extracted pages from cache")
        coords = coordcheck.readings(coordcheck.targets([p for ps in rendered.values() for p in ps], extractions),
                                     llm.cached_coordinates)
        _store(con, docs, rendered, classes, extractions, labs, dry_run=dry_run, incomplete_docs=truncated,
               coords=coords)
    finally:
        con.close()


def cached_inputs(rendered: Rendered) -> tuple[Classes, dict[tuple[str, int], PageLabResults]]:
    """Cached classifications and lab-sheet results of the rendered pages."""
    classes = {(p.doc_id, p.page): c for ps in rendered.values() for p in ps
               if (c := llm.cached_classification(p)) is not None}
    labs = {(p.doc_id, p.page): x for ps in rendered.values() for p in ps
            if (c := classes.get((p.doc_id, p.page))) and c.kind.value == "lab_results"
            and (x := llm.cached_lab_results(p)) is not None}
    return classes, labs


def extract_todo(rendered: Rendered, classes: Classes) -> list[ingest.PageImage]:
    return [p for ps in rendered.values() for p in ps
            if (c := classes.get((p.doc_id, p.page))) and c.kind.value in EXTRACT_KINDS]


def _lost_logs(pages: list[ingest.PageImage], classes: Classes, extractions: Extractions
               ) -> list[tuple[int, str, str | None]]:
    """Log pages that came back with fewer logs than the classification counted, and holes that came back without
    a layer on any page: (page, finding, hole name). Counted, not matched by name: the classification reads the
    thumbnail and often writes a hole's name differently. A log without layers counts on its page: a last sheet
    often holds only notes or the end-of-hole statement. Probe records (dynamic probe, CPT) are left out, they
    often carry blow counts only."""
    lost, first, layered = [], {}, set()
    for p in pages:
        c = classes[(p.doc_id, p.page)]
        if not (expected := expected_logs(c)):
            continue
        logs = extractions[(p.doc_id, p.page)].logs
        if (found := len(logs)) < expected:
            lost.append((p.page, f"log not extracted: page {p.page} shows {expected} logs, {found} came back", None))
        for log in logs:
            kind, number = log_key(log, c.kind.value)
            if kind != "probe":
                first.setdefault(number, (p.page, log.name))
                if log.layers:
                    layered.add(number)
    lost += [(page, f"log not extracted: {name} came back without layers on every page", name)
             for number, (page, name) in first.items() if number not in layered]
    return lost


def _has_previous(con: sqlite3.Connection, doc_ids: Iterable[str]) -> bool:
    return any(con.execute("SELECT 1 FROM boreholes WHERE doc_id = ? LIMIT 1", (d,)).fetchone() for d in doc_ids)


def _complete(con: sqlite3.Connection, doc_id: str, pages: list[ingest.PageImage], classes: Classes,
              extractions: Extractions) -> bool:
    available = all((p.doc_id, p.page) in classes for p in pages) and all(
        (p.doc_id, p.page) in extractions
        for p in pages
        if classes[(p.doc_id, p.page)].kind.value in EXTRACT_KINDS
    )
    if not available:
        return False
    # Earlier records of the document stay rather than lose a hole; without any, the document is stored
    # with a blocking placeholder per lost page, so a fresh database does not silently miss it.
    return not (_lost_logs(pages, classes, extractions) and _has_previous(con, [doc_id]))


def _publishable_docs(con: sqlite3.Connection, rendered: Rendered, classes: Classes, extractions: Extractions,
                      incomplete_docs: set[str], dry_run: bool) -> list[str]:
    """Documents whose records can replace the stored ones: complete, in a group without incomplete documents.
    A dry run writes nothing, so it shows every classified document."""
    def complete(doc_id: str) -> bool:
        return doc_id not in incomplete_docs and _complete(con, doc_id, rendered[doc_id], classes, extractions)

    if dry_run:
        done = [doc_id for doc_id in rendered if all((p.doc_id, p.page) in classes for p in rendered[doc_id])]
        partial = sorted(d for d in done if not complete(d))
        if partial:
            print(f"  dry run includes documents still being extracted: {', '.join(partial)}")
    else:
        done = [doc_id for doc_id in rendered if complete(doc_id)]
        incomplete_groups = {store.document_group(d) for d in set(rendered) - set(done)}
        done = [d for d in done if store.document_group(d) not in incomplete_groups]
    skipped = sorted(set(rendered) - set(done))
    if skipped:
        print(f"  ! kept previous results for incomplete documents: {', '.join(skipped)}", file=sys.stderr)
    return done


def _protected(con: sqlite3.Connection, records: list[Borehole], uids: dict[str, str | None],
               fixes: dict[str, list]) -> dict[str, set]:
    """Identities a reviewer stands behind, per record that has corrections or a human decision: deduplication
    must not merge them away. The record's own id counts when the decision or a correction names it. Document
    corrections apply to every hole of the group, so they identify none."""
    human = {s.value for s in store.HUMAN}
    protected = {}
    for bh in records:
        uid = uids[bh.borehole_id]
        own = [f for f in fixes[bh.borehole_id] if f["scope"] == "borehole"]
        status = con.execute("SELECT review_status FROM boreholes WHERE id = ? OR uid = ?",
                             (bh.borehole_id, uid)).fetchone()
        if not (own or status and status[0] in human):
            continue
        decided_here = con.execute("SELECT review_status FROM boreholes WHERE id = ?", (bh.borehole_id,)).fetchone()
        reviewed_as = (decided_here and decided_here[0] in human) or any(f["target"] == bh.borehole_id for f in own)
        protected[bh.borehole_id] = {uid} | {f["id"] for f in own} | ({bh.borehole_id} if reviewed_as else set())
    return protected


def _write_group(con: sqlite3.Connection, records: list[Borehole], doc_ids: list[str], dry_run: bool) -> list[Borehole]:
    """Read knowledge and corrections and replace one document group's records in one short write transaction:
    a correction saved meanwhile cannot be lost, and reviewers wait for one group at most."""
    con.execute("BEGIN IMMEDIATE")
    try:
        uids = {bh.borehole_id: store.uid_for(con, bh) for bh in records}
        fixes = {bh.borehole_id: store.corrections_for(con, bh.borehole_id, uids[bh.borehole_id]) for bh in records}
        protected = _protected(con, records, uids, fixes)
        # Copies of a hole are judged as they will be stored, with knowledge and corrections, but merged before
        # either applies, so a correction of a lab value finds the values moved in from a copy. Independent
        # groups do not share an identity scope; coincident positions alone cannot join them.
        judged = [bh.model_copy(deep=True) for bh in records]
        knowledge.apply_all(con, judged)
        for bh in judged:
            corrections.apply(bh, fixes[bh.borehole_id])
        records = dedupe(records, protected, {bh.borehole_id: bh for bh in judged})
        # The model's output for `eval`: after merging duplicates, which moves lab values, before reviewer input.
        extracted = {bh.borehole_id: evaluate.extracted_summary(bh) for bh in records}
        knowledge.apply_all(con, records)
        unstack_place_positions(records, protected)
        for bh in records:  # reviewer fixes survive re-extraction, and win over learned knowledge
            corrections.apply(bh, fixes[bh.borehole_id])
        # A reviewer's CRS applied to misread digits can move a hole far off: positions are checked as stored.
        for bh in flag_position_outliers(records):
            corrections.rescore(bh)
        if dry_run:
            con.rollback()
            return records
        previous: dict[str, dict] = {}
        for doc_id in doc_ids:
            previous.update(store.delete_document_boreholes(con, doc_id))
        for bh in records:
            store.save_borehole(con, bh, commit=False, previous=previous.get(bh.borehole_id),
                                extracted=extracted[bh.borehole_id])
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return records


def _store(con: sqlite3.Connection, docs: list[ingest.SourceDoc], rendered: Rendered, classes: Classes,
           extractions: Extractions, labs: dict[tuple[str, int], PageLabResults] | None = None,
           dry_run: bool = False, incomplete_docs: set[str] | None = None,
           coords: dict[coordcheck.Key, CoordinateReading | None] | None = None) -> set[str]:
    """Lab sheets are optional: a failed lab page does not hold back the document's logs.
    dry_run: assemble and report, but write nothing. coords: second readings of printed coordinates."""
    labs = labs or {}
    source_file = {d.doc_id: d.path for d in docs}
    done = _publishable_docs(con, rendered, classes, extractions, incomplete_docs or set(), dry_run)
    results = [
        PageResult(p.doc_id, source_file[p.doc_id], p.page, classes[(p.doc_id, p.page)].kind.value,
                   extractions.get((p.doc_id, p.page)), p.text, classes[(p.doc_id, p.page)].crs_text,
                   labs.get((p.doc_id, p.page)))
        for doc_id in done for p in rendered[doc_id]
    ]
    failed_docs: set[str] = set()
    boreholes = assemble(results, deduplicate=False, failed_docs=failed_docs)
    if failed_docs and not dry_run:
        # A hole that failed to assemble is a blocking placeholder; earlier records of its group stay instead.
        failed_groups = {g for g in {store.document_group(d) for d in failed_docs}
                         if _has_previous(con, (d for d in done if store.document_group(d) == g))}
        done = [d for d in done if store.document_group(d) not in failed_groups]
        boreholes = [bh for bh in boreholes if store.document_group(bh.borehole_id) not in failed_groups]
        if failed_groups:
            print(f"  ! kept previous results for {len(failed_groups)} groups with assembly errors", file=sys.stderr)
    if not dry_run:
        for doc_id in done:
            for page, issue, name in _lost_logs(rendered[doc_id], classes, extractions):
                # A record read from that page may hold the missing hole's layers: it is blocked as well.
                for bh in boreholes:
                    if document_id(bh.borehole_id) == doc_id and any(s.page == page for s in bh.sources):
                        bh.issues.append(issue)
                        corrections.rescore(bh)
                boreholes.append(placeholder(doc_id, source_file[doc_id], page, issue, name))
                print(f"  ! {doc_id}: {issue}", file=sys.stderr)
    for bh in boreholes:
        if coords and (note := coordcheck.check(bh, coords)):
            bh.issues.append(note)
            corrections.rescore(bh)
    groups: dict[str, list[Borehole]] = {store.document_group(doc_id): [] for doc_id in done}
    for bh in boreholes:
        groups.setdefault(store.document_group(bh.borehole_id), []).append(bh)
    stored = []
    for group, records in groups.items():
        stored += _write_group(con, records, [d for d in done if store.document_group(d) == group], dry_run)
    if dry_run:
        _report(stored, len(done))
        return set(done)
    orphans = store.orphan_corrections(con)
    if orphans:
        print(f"  ! {len(orphans)} reviewer corrections belong to holes no current record carries "
              f"(e.g. {orphans[0]['target']}); see /api/corrections/orphans", file=sys.stderr)
    located = sum(1 for b in stored if b.location.lat is not None)
    auto = sum(1 for b in stored if b.review_status.value == "auto_approved")
    print(f"Stored {len(stored)} boreholes ({located} georeferenced) from {len(done)} documents in {config.DB_PATH}")
    print(f"  released automatically: {auto}, waiting for review: {len(stored) - auto} "
          f"(threshold {round(config.AUTO_APPROVE_THRESHOLD * 100)})")
    return set(done)


def _report(boreholes: list[Borehole], n_docs: int) -> None:
    """One line per record: what a run would store (for checking results while another run writes)."""
    for bh in sorted(boreholes, key=lambda b: b.borehole_id):
        findings = [i for i in bh.issues if not i.startswith("unreadable")]
        print(f"  {bh.borehole_id:<28} {bh.review_status.value:<15} score {round((bh.score or 0) * 100):>3}  "
              f"{len(bh.layers):>2} layers  {len(bh.lab_results):>3} lab  | {'; '.join(findings[:3])[:150]}")
    auto = sum(1 for b in boreholes if b.review_status.value == "auto_approved")
    print(f"Dry run: {len(boreholes)} boreholes from {n_docs} documents, {auto} would be released automatically; "
          "nothing written")


def run_experiment(action: str, name: str | None, doc_ids: list[str] | None, yes: bool,
                   reference_pages: bool) -> None:
    """Extract the reference documents (or `doc_ids`) into an isolated arm and store them in its own DB.
    'baseline' takes the main cache; 'run' uses the arm's prompt."""
    reference = evaluate.load_reference()
    if not doc_ids and not reference:
        raise RuntimeError("no reference yet: approve records, run `python -m pipeline eval snapshot`, or pass --doc")
    docs = select(doc_ids or sorted({store.document_group(r["doc_id"]) for r in reference.values()}))
    rendered = {d.doc_id: ingest.render(d) for d in docs}
    classes, labs = cached_inputs(rendered)  # from the main cache, before switching to the arm's cache
    unclassified = sum((p.doc_id, p.page) not in classes for ps in rendered.values() for p in ps)
    if unclassified:
        raise RuntimeError(f"{unclassified} pages have no cached classification: run `python -m pipeline run` on "
                           "these documents first")
    todo = extract_todo(rendered, classes)
    if action == "baseline":
        name = experiment.BASELINE
        extractions = _cached_extractions(_chains(todo, classes), classes)
        coords = coordcheck.readings(coordcheck.targets(todo, extractions), llm.cached_coordinates)
        con = experiment.fresh_db(name)
    else:
        kept, kept_coords = {}, {}
        if reference_pages:
            # Re-extract whole chains so continuation context stays within one prompt; the rest keeps the
            # main cache's pages, which the reference records do not come from.
            con = store.connect()
            wanted = experiment.reference_pages(con, reference)
            con.close()
            path_of = {d.doc_id: d.path for d in docs}
            chains = _chains(todo, classes)
            rerun = [any((path_of[p.doc_id], p.page) in wanted for p in ch) for ch in chains]
            todo = [p for ch, r in zip(chains, rerun) if r for p in ch]
            kept = _cached_extractions([ch for ch, r in zip(chains, rerun) if not r], classes)
            # The kept pages' second readings come from the main cache too, as for the baseline.
            kept_coords = coordcheck.readings(coordcheck.targets([p for ch in chains for p in ch], kept),
                                              llm.cached_coordinates)
        fingerprint = experiment.use_prompt(name)
        new = _uncached(todo, classes, retry=yes)
        if new and not yes:
            raise RuntimeError(f"{len(new)} of {len(todo)} pages to extract with prompt {name} ({fingerprint}), "
                               f"≈ ${len(new) * page_cost(COST_EXTRACT, config.EXTRACT_MODEL):.2f} on the API or "
                               "subscription quota. Re-run with --yes.")
        if new and (msg := missing_credentials()):
            raise RuntimeError(msg)
        # No DB: no reviewer knowledge in the prompt. Without --yes nothing is called that is not cached yet.
        extracted, failed = _extract(todo, classes, retry=yes)
        # A partial arm would score its failed pages as missing records; its successful pages stay cached.
        if failed:
            raise RuntimeError(f"{failed} of {len(todo)} pages failed: arm {name} left unchanged, re-run to retry them")
        coords = kept_coords | coordcheck.readings(coordcheck.targets(todo, extracted),
                                                   llm.read_coordinates if yes else llm.cached_coordinates)
        con = experiment.fresh_db(name)
        extractions = kept | extracted
    _store(con, docs, rendered, classes, extractions, labs, coords=coords)
    experiment.save_proxy(name, experiment.proxy(con, docs, rendered))
    con.close()
