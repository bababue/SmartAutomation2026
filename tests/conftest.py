import pytest

from pipeline import config


@pytest.fixture(autouse=True)
def no_gazetteer(tmp_path, monkeypatch):
    """Tests run without the downloaded gazetteer unless they build their own (tests/test_gazetteer.py)."""
    monkeypatch.setattr(config, "GAZETTEER_PATH", tmp_path / "no-gazetteer.sqlite")


@pytest.fixture(autouse=True)
def no_second_coordinate_reading(monkeypatch):
    """`run` reads printed coordinates a second time; tests never call a model."""
    from pipeline import llm
    monkeypatch.setattr(llm, "read_coordinates", lambda *args: None)
