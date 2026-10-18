"""The `claude-code` CLI backend (no LLM call; the CLI invocation is mocked). It must decode the subprocess
output as UTF-8 explicitly: Windows' default `subprocess.run(text=True)` encoding is the OS locale (e.g. cp1252),
which silently garbles non-ASCII text the CLI prints as UTF-8. Its Read tool must not reach the repo (`.env`,
the dataset)."""

import json
import subprocess

from pipeline import llm
from pipeline.extraction_models import PageClassification


def test_cli_backend_requests_utf8_decoding(tmp_path, monkeypatch):
    calls = []

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = json.dumps({
            "is_error": False,
            "structured_output": {
                "kind": "other", "logs": [], "language": "de", "has_coordinates": False, "crs_text": None,
            },
        }, ensure_ascii=False)

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    image = tmp_path / "p.jpg"
    image.write_bytes(b"x")

    result = llm._call_claude_code("claude-opus-5-5", "system", image, "München", PageClassification)

    assert result.language == "de"
    assert calls[0]["encoding"] == "utf-8"


def test_cli_backend_reads_only_a_folder_holding_the_page(tmp_path, monkeypatch):
    from pathlib import Path

    from pipeline import config
    seen = {}

    def fake_run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        seen.update(cwd=cwd, files=sorted(p.name for p in cwd.iterdir()), cmd=cmd)
        return type("Proc", (), {"returncode": 0, "stderr": "", "stdout": json.dumps({"structured_output": {
            "kind": "other", "logs": [], "language": None, "has_coordinates": False, "crs_text": None}})})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    image = tmp_path / "0001.jpg"
    image.write_bytes(b"x")

    llm._call_claude_code("claude-opus-5-5", "system", image, "", PageClassification)

    assert seen["cwd"] != config.ROOT.resolve() and seen["files"] == ["page.jpg"]
    assert f"{seen['cwd'] / 'page.jpg'} " in seen["cmd"][2]
    assert "--restricted" in seen["cmd"] and "--add-dir" not in seen["cmd"]
    assert not seen["cwd"].exists()
