import argparse

from pipeline import config
from pipeline.__main__ import cmd_share


def test_package_carries_reference_and_audit_but_not_experiments(db_path, tmp_path, monkeypatch):
    eval_dir = tmp_path / "eval"
    (eval_dir / "experiments" / "arm").mkdir(parents=True)
    (eval_dir / "experiments" / "arm" / "boreholes.sqlite").write_bytes(b"")
    (eval_dir / "reference.json").write_text('{"records": {}}')
    (eval_dir / "audit.json").write_text('{"records": []}')
    monkeypatch.setattr(config, "REFERENCE_PATH", eval_dir / "reference.json")
    monkeypatch.setattr(config, "AUDIT_PATH", eval_dir / "audit.json")

    out = tmp_path / "package"
    cmd_share(argparse.Namespace(out=str(out), no_pages=True, with_cache=False))

    assert sorted(p.name for p in (out / "eval").iterdir()) == ["audit.json", "reference.json"]
    assert (out / "eval" / "reference.json").read_text() == '{"records": {}}'
    assert "eval/ -> data/eval/" in (out / "README.md").read_text()
