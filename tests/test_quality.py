"""Model evaluation on the statistics page: the committed snapshot and the fallback to it."""

import json

from fastapi.testclient import TestClient

from api import models
from api.main import app
from pipeline import config

TEXT_FIELDS = {"generated", "created", "date", "model", "arm", "name", "prompt", "against", "route"}


def test_snapshot_holds_declared_aggregates_only():
    data = json.loads(config.MODEL_EVALUATION_PATH.read_text(encoding="utf-8"))
    models.ModelEvaluation.model_validate(data)  # extra="forbid": an undeclared field fails

    def texts(value, key=None):
        if isinstance(value, dict):
            for k, v in value.items():
                yield from texts(v, k)
        elif isinstance(value, list):
            for v in value:
                yield from texts(v, key)
        elif isinstance(value, str):
            yield key

    assert set(texts(data)) <= TEXT_FIELDS


def test_quality_falls_back_to_the_snapshot_without_eval_data(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_PATH", tmp_path / "eval" / "reference.json")
    body = TestClient(app).get("/api/stats/quality").json()
    assert body["source"] == "snapshot"
    assert body["evaluation"] == json.loads(config.MODEL_EVALUATION_PATH.read_text(encoding="utf-8"))
    assert body["usage"] == []
