"""Document replacement must preserve human work when processing is incomplete."""

import json

import pytest

from pipeline import config, store
from pipeline import __main__ as cli
from pipeline.extraction_models import LogRef, PageClassification, PageExtraction
from pipeline.ingest import PageImage, SourceDoc
from schema.borehole import ReviewStatus
from tests.test_assemble import _log
from tests.test_store import record


@pytest.fixture()
def con(tmp_path, monkeypatch):
    path = tmp_path / "publish.sqlite"
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(store.connect, "__defaults__", (path,))
    connection = store.connect(path)
    yield connection
    connection.close()


def inputs(doc_id="report", names=("BH1",)):
    doc = SourceDoc(doc_id, f"{doc_id}.pdf", "pdf")
    page = PageImage(doc_id, 1, "", "", "", 1, 1)
    classification = PageClassification(kind="borehole_log", logs=[LogRef(name=n, sheet=None) for n in names],
                                        language="en", has_coordinates=True, crs_text=None)
    extraction = PageExtraction(logs=[_log(n, "E 412000 N 5320000", 300, [(0, 9)]) for n in names],
                                coordinate_rows=[])
    return [doc], {doc_id: [page]}, {(doc_id, 1): classification}, {(doc_id, 1): extraction}


@pytest.mark.parametrize("missing", ["empty", "one_hole", "assembly_error"])
def test_incomplete_inventory_preserves_records_and_decisions(con, monkeypatch, missing):
    args = inputs(names=("BH1", "BH2"))
    for name in ("BH1", "BH2"):
        store.save_borehole(con, record(bid=f"report:{name}", name=name))
    store.record_review(con, "report:BH1", ReviewStatus.approved, "Ana", None)
    if missing == "assembly_error":
        import pipeline.assemble as assembly
        build = assembly.build_borehole

        def fail_second(doc_id, source, logs, *args):
            if logs[0][0].name == "BH2":
                raise ValueError("unreadable synthetic result")
            return build(doc_id, source, logs, *args)

        monkeypatch.setattr(assembly, "build_borehole", fail_second)
    else:
        args[3][("report", 1)].logs = args[3][("report", 1)].logs[:0 if missing == "empty" else 1]
    assert cli._store(con, *args) == set()
    assert store.load_borehole(con, "report:BH1").review_status == ReviewStatus.approved
    assert store.load_borehole(con, "report:BH2") is not None


def test_complete_inventory_replaces_document(con):
    store.save_borehole(con, record(bid="report:old", name="old"))
    assert cli._store(con, *inputs()) == {"report"}
    assert store.load_borehole(con, "report:old") is None
    assert store.load_borehole(con, "report:BH1") is not None


def test_partial_document_holds_back_its_group(con):
    docs, rendered, classes, extractions = inputs("folder__a")
    other = inputs("folder__b")
    docs += other[0]
    rendered.update(other[1])
    classes.update(other[2])
    extractions.update(other[3])
    assert cli._store(con, docs, rendered, classes, extractions, incomplete_docs={"folder__b"}) == set()
    assert con.execute("SELECT COUNT(*) FROM boreholes").fetchone()[0] == 0


@pytest.mark.parametrize("command", ["run", "rebuild"])
def test_page_cap_preserves_records_from_omitted_pages(con, monkeypatch, command):
    docs, rendered, classes, extractions = inputs()
    rendered["report"].append(PageImage("report", 2, "", "", "", 1, 1))
    store.save_borehole(con, record(bid="report:BH2", name="BH2"))
    monkeypatch.setattr(cli, "_missing_credentials", lambda: None)
    monkeypatch.setattr(cli, "_ingest", lambda *args: rendered)
    monkeypatch.setattr(cli.ingest, "render", lambda *args: rendered["report"])
    monkeypatch.setattr(cli, "_parallel", lambda fn, pages, label: classes if label == "classify" else {})
    monkeypatch.setattr(cli, "_extract", lambda *args: (extractions, 0))
    monkeypatch.setattr(cli, "cached_inputs", lambda *args: (classes, {}))
    monkeypatch.setattr(cli.llm, "cached_extraction", lambda *args: extractions[("report", 1)])
    if command == "run":
        with pytest.raises(RuntimeError, match="incomplete"):
            cli.run(docs, max_pages=1)
    else:
        cli.rebuild(docs, max_pages=1)
    assert store.load_borehole(con, "report:BH2") is not None


def test_dedupe_keeps_corrected_and_reviewed_records(con, monkeypatch):
    docs, rendered, classes, extractions = inputs("folder__a")
    other = inputs("folder__b")
    docs += other[0]
    rendered.update(other[1])
    classes.update(other[2])
    extractions.update(other[3])
    a = record(bid="folder__a:BH1", name="BH1")
    b = record(bid="folder__b:BH1", name="BH1")
    store.save_borehole(con, b)
    store.record_review(con, b.borehole_id, ReviewStatus.rejected, "Ana", "check material")
    store.add_correction(con, "borehole", b.borehole_id, "layer",
                         {"orig_top": 0, "orig_bottom": 4.5, "primary": "fine_sand"}, "Ana", None)
    con.commit()
    monkeypatch.setattr(cli, "assemble", lambda *args, **kwargs: [a, b])
    cli._store(con, docs, rendered, classes, extractions)
    assert store.load_borehole(con, b.borehole_id).layers[0].primary.value == "fine_sand"
    assert store.load_borehole(con, a.borehole_id) is not None
    assert store.orphan_corrections(con) == []


def test_dedupe_prefers_the_better_position():
    from pipeline.assemble import dedupe
    a, b = record(bid="a:BH1"), record(bid="b:BH1")
    a.confidence, a.location.confidence, a.review_status = 1.0, 0.5, ReviewStatus.pending_review
    b.confidence, b.location.confidence, b.review_status = 0.95, 0.95, ReviewStatus.auto_approved
    assert dedupe([a, b]) == [b]


@pytest.mark.parametrize("field,first,second", [("relative_density", "dense", "loose"),
                                               ("weathering", "W2", "W4"), ("color", "grey", "brown")])
def test_continued_layers_keep_distinct_properties(field, first, second):
    from pipeline.assemble import _layers
    from schema.borehole import SourceRef
    a, b = _log("BH1", None, None, [(0, 10)]), _log("BH1", None, None, [(10, 20)])
    a.layers[0] = a.layers[0].model_copy(update={field: first, "description_original": "SAND"})
    b.layers[0] = b.layers[0].model_copy(update={field: second, "description_original": f"SAND, {second} (continued)"})
    layers = _layers([(a, SourceRef(file="r.pdf", page=1)), (b, SourceRef(file="r.pdf", page=2))], 20)
    assert [(l.top_m, l.bottom_m, getattr(l, field)) for l in layers] == [(0, 10, first), (10, 20, second)]


@pytest.mark.parametrize("text", ["Clayey SAND, grey", "As above"])
def test_one_text_over_a_sheet_break_is_one_layer_however_its_properties_were_read(text):
    from pipeline.assemble import _layers
    from schema.borehole import SourceRef
    a, b = _log("BH1", None, None, [(0, 8)]), _log("BH1", None, None, [(8, 12)])
    a.layers[0] = a.layers[0].model_copy(update={"description_original": "Clayey SAND, grey", "moisture": "M",
                                                 "geologic_age": "Quaternary"})
    b.layers[0] = b.layers[0].model_copy(update={"description_original": text, "moisture": "W", "geologic_age": None})
    layers = _layers([(a, SourceRef(file="r.pdf", page=1)), (b, SourceRef(file="r.pdf", page=2))], 12)
    assert [(l.top_m, l.bottom_m) for l in layers] == [(0, 12)]


@pytest.mark.parametrize("second_interval", [(0, 10), (0, 15), (5, 10), (0, 5)])
def test_restated_layers_do_not_discard_conflicting_properties(second_interval):
    from pipeline.assemble import _layers
    from schema.borehole import SourceRef
    a, b = _log("BH1", None, None, [(0, 10)]), _log("BH1", None, None, [second_interval])
    a.layers[0] = a.layers[0].model_copy(update={"relative_density": "dense"})
    b.layers[0] = b.layers[0].model_copy(update={"relative_density": "loose"})
    layers = _layers([(a, SourceRef(file="r.pdf", page=1)), (b, SourceRef(file="r.pdf", page=2))], 15)
    assert [l.relative_density for l in layers] == ["dense", "loose"]


def test_independent_document_groups_keep_their_own_identity(con, monkeypatch):
    docs, rendered, classes, extractions = inputs("a")
    other = inputs("b")
    docs += other[0]
    rendered.update(other[1])
    classes.update(other[2])
    extractions.update(other[3])
    monkeypatch.setattr(cli, "assemble", lambda *args, **kwargs: [record(bid="a:BH1"), record(bid="b:BH1")])
    cli._store(con, docs, rendered, classes, extractions)
    assert con.execute("SELECT COUNT(*) FROM boreholes").fetchone()[0] == 2


@pytest.mark.parametrize("max_pages", [0, -1])
def test_invalid_page_cap_is_rejected_before_processing(monkeypatch, max_pages):
    monkeypatch.setattr(cli, "_missing_credentials", lambda: pytest.fail("credentials should not be checked"))
    for command in (cli.run, cli.rebuild):
        with pytest.raises(ValueError, match="positive"):
            command([], max_pages=max_pages)


def test_fresh_database_stores_a_document_with_a_lost_hole_as_blocking(con):
    args = inputs(names=("BH1", "BH2"))
    args[3][("report", 1)].logs = args[3][("report", 1)].logs[:1]
    assert cli._store(con, *args) == {"report"}
    bh = store.load_borehole(con, "report:BH1")
    assert "log not extracted: page 1 shows 2 logs, 1 came back" in bh.issues
    assert bh.review_status == ReviewStatus.pending_review


def test_eval_snapshot_keeps_lab_values_merged_from_a_duplicate(con, monkeypatch):
    docs, rendered, classes, extractions = inputs("folder__a")
    other = inputs("folder__b")
    docs += other[0]
    rendered.update(other[1])
    classes.update(other[2])
    extractions.update(other[3])
    a = record(bid="folder__a:BH1", name="BH1", ucs=42.0)
    b = record(bid="folder__b:BH1", name="BH1", ucs=17.0)
    monkeypatch.setattr(cli, "assemble", lambda *args, **kwargs: [a, b])
    cli._store(con, docs, rendered, classes, extractions)
    (kept, extracted), = con.execute("SELECT id, extracted FROM boreholes").fetchall()
    assert sorted(value for _, value, *_ in json.loads(extracted)["lab"]) == [17.0, 42.0]


def test_last_sheet_with_notes_only_is_not_a_lost_log(con):
    docs, rendered, classes, extractions = inputs()
    rendered["report"].append(PageImage("report", 2, "", "", "", 1, 1))
    classes[("report", 2)] = classes[("report", 1)]
    end_sheet = _log("BH1", "E 412000 N 5320000", 300, [])
    extractions[("report", 2)] = PageExtraction(logs=[end_sheet], coordinate_rows=[])
    assert cli._store(con, docs, rendered, classes, extractions) == {"report"}
    assert not any(i.startswith("log not extracted") for i in store.load_borehole(con, "report:BH1").issues)
