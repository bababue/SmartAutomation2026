"""Prompt experiments stay isolated from the main cache and database."""

from pipeline import config, experiment, llm
from pipeline.ingest import PageImage


def test_arm_uses_its_own_cache_and_a_key_from_its_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENTS_DIR", tmp_path / "experiments")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "PROMPT_VERSION", config.PROMPT_VERSION)
    monkeypatch.setattr(config, "DB_PATH", config.DB_PATH)
    monkeypatch.setattr(llm, "EXTRACT_SYSTEM", llm.EXTRACT_SYSTEM)
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "p.jpg").write_bytes(b"jpg")
    page = PageImage(doc_id="DOC1", page=1, image="p.jpg", thumb="p.jpg", width=1, height=1, text="")
    main_key, main_path = llm._extract_key(page, "borehole_log", "", "m", "e"), llm.cache_path("extract", page)

    prompt = experiment.init("try-units")
    assert prompt.read_text(encoding="utf-8") == llm.EXTRACT_SYSTEM
    experiment.use_prompt("try-units")
    same_text_key = llm._extract_key(page, "borehole_log", "", "m", "e")
    assert llm.cache_path("extract", page) != main_path and main_path.parent.parent.parent == tmp_path / "cache"
    assert same_text_key != main_key  # never reads a page extracted under the main prompt version

    prompt.write_text(prompt.read_text(encoding="utf-8") + "\n- One more rule.", encoding="utf-8")
    experiment.use_prompt("try-units")
    assert llm.EXTRACT_SYSTEM.endswith("One more rule.") and llm._extract_key(page, "borehole_log", "", "m", "e") != same_text_key

    con = experiment.fresh_db("try-units")
    assert config.DB_PATH == tmp_path / "experiments" / "try-units" / "boreholes.sqlite"
    con.close()


def test_cost_gate_counts_a_stale_cache_entry_as_a_call(tmp_path, monkeypatch):
    import json

    from pipeline import runner
    from pipeline.extraction_models import PageClassification
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "p.jpg").write_bytes(b"jpg")
    page = PageImage(doc_id="DOC1", page=1, image="p.jpg", thumb="p.jpg", width=1, height=1, text="")
    classes = {("DOC1", 1): PageClassification(kind="coordinate_table", logs=[], language=None, has_coordinates=True,
                                               crs_text=None)}
    path = llm.cache_path("extract", page)
    path.parent.mkdir(parents=True)

    def cache(key):
        path.write_text(json.dumps({"key": key, "model": config.EXTRACT_MODEL, "effort": config.EXTRACT_EFFORT,
                                    "result": {"logs": [], "coordinate_rows": []}}), encoding="utf-8")

    cache("made with an earlier prompt")
    assert runner._uncached([page], classes, retry=False) == [page]
    cache(llm._extract_key(page, "coordinate_table", "", config.EXTRACT_MODEL, config.EXTRACT_EFFORT))
    assert runner._uncached([page], classes, retry=False) == []
