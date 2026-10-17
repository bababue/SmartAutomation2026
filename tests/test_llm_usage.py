"""Token usage recorded with cached LLM answers, and the cost derived from it (no LLM calls)."""

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

    llm._cached("classify", page, "k", PageClassification, produce)
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
    produce = lambda: llm._call_openai("vendor/model", "system", image, "hint", PageClassification, 2000)
    result = llm._cached("classify", page, "k", PageClassification, produce)
    assert result.kind.value == "other"
    assert len(sent[1]["messages"]) == 4  # the invalid answer and the validation error went back once
    (n, usd), = llm.measured_costs().values()
    assert (n, usd) == (1, pytest.approx(0.02))
