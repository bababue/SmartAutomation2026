"""Token usage recorded with cached LLM answers, and the cost derived from it (no LLM calls)."""

import json
from types import SimpleNamespace

import pytest

from pipeline import config, llm
from pipeline.extraction_models import PageClassification
from pipeline.ingest import PageImage


def response(model: str, inp: int, out: int, write: int = 0, read: int = 0):
    usage = SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=write,
                            cache_read_input_tokens=read)
    return SimpleNamespace(model=model, usage=usage)


def test_usage_of_all_rounds_is_cached_and_priced(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    page = PageImage(doc_id="d", page=1, image="", thumb="", text="", width=1, height=1)

    def produce():
        llm._anthropic_usage(response("claude-opus-5-5", 1_000, 500, write=2_000))
        llm._anthropic_usage(response("claude-opus-5-5", 1_000, 500, read=2_000))  # repair round
        return PageClassification(kind="other", logs=[], language=None, has_coordinates=False, crs_text=None)

    llm._cached("classify", page, lambda m, e: "k", "m", "e", PageClassification, produce)
    (n, usd), = llm.measured_costs().values()
    # 2000 in + 1000 out + 2000 cache write + 2000 cache read at $4 / $20 per million
    assert n == 1
    assert usd == pytest.approx((2_000 * 4 + 1_000 * 20 + 2_000 * 5 + 2_000 * 0.4) / 1e6)


def test_unknown_model_has_no_price():
    tokens = dict.fromkeys(llm.USAGE_FIELDS, 10)
    assert llm.cost_usd({"some-other-model": tokens}) is None



def test_openai_compatible_call_repairs_once_and_keeps_billed_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    image = tmp_path / "p.jpg"
    image.write_bytes(b"\xff\xd8")
    page = PageImage(doc_id="d", page=1, image="", thumb="", text="", width=1, height=1)
    valid = '{"kind": "other", "logs": [], "language": null, "has_coordinates": false, "crs_text": null}'
    answers = iter(['{"kind": "other"}', f"```json\n{valid}\n```"])
    sent = []

    def post_chat(body):
        sent.append(body)
        return {"model": "vendor/model", "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.01},
                "choices": [{"finish_reason": "stop", "message": {"content": next(answers)}}]}

    monkeypatch.setattr(llm, "_post_chat", post_chat)
    produce = lambda: llm._call_openai("vendor/model", "system", image, "hint", PageClassification, 2000, True)
    result = llm._cached("classify", page, lambda m, e: "k", "m", "e", PageClassification, produce)
    assert result.kind.value == "other"
    assert len(sent[1]["messages"]) == 4  # the invalid answer and the validation error went back once
    (n, usd), = llm.measured_costs().values()
    assert (n, usd) == (1, pytest.approx(0.02))


def test_rebuild_keeps_pages_of_the_previous_model_and_a_run_replaces_them(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path)
    (tmp_path / "t.jpg").write_bytes(b"\xff\xd8")
    page = PageImage(doc_id="d", page=1, image="t.jpg", thumb="t.jpg", text="", width=1, height=1)
    answer = lambda kind: lambda: PageClassification(kind=kind, logs=[], language=None, has_coordinates=False,
                                                     crs_text=None)
    monkeypatch.setattr(config, "CLASSIFY_MODEL", "old-model")
    monkeypatch.setattr(llm, "_produce", lambda *a, **k: answer("borehole_log"))
    llm.classify_page(page)

    monkeypatch.setattr(config, "CLASSIFY_MODEL", "new-model")
    assert llm.cached_classification(page).kind.value == "borehole_log"  # rebuild: the old model's page

    monkeypatch.setattr(llm, "_produce", lambda *a, **k: answer("other"))
    assert llm.classify_page(page).kind.value == "other"  # run: the configured model extracts anew
    assert json.loads(llm.cache_path("classify", page).read_text())["model"] == "new-model"


def test_guard_limits_and_records_calls_but_not_cache_hits(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    first, second = (PageImage(doc_id="d", page=n, image="", thumb="", text="", width=1, height=1) for n in (1, 2))
    calls = []

    def produce():
        calls.append(1)
        return PageClassification(kind="other", logs=[], language=None, has_coordinates=False, crs_text=None)

    produce.sent = {"image_bytes": 1234, "text_chars": 56}
    rows = []
    guard = llm.CallGuard(max_calls=1, record=rows.append)
    with llm.guarded(["d"], guard):
        llm._cached("classify", first, lambda m, e: "k", "m", "e", PageClassification, produce)
        llm._cached("classify", first, lambda m, e: "k", "m", "e", PageClassification, produce)  # cached
        with pytest.raises(llm.CallLimitReached):
            llm._cached("classify", second, lambda m, e: "k", "m", "e", PageClassification, produce)
    assert len(calls) == 1 and guard.exhausted
    (row,) = rows
    assert (row["doc_id"], row["page"], row["stage"], row["outcome"]) == ("d", 1, "classify", "ok")
    assert (row["image_bytes"], row["text_chars"]) == (1234, 56)

    stopped = llm.CallGuard(cancelled=lambda doc_id: doc_id == "d")
    with llm.guarded(["d"], stopped), pytest.raises(llm.Cancelled):
        llm._cached("classify", second, lambda m, e: "k2", "m", "e", PageClassification, produce)
    assert len(calls) == 1  # no call for a cancelled document
