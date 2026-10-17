"""The `claude-code` CLI backend must decode the subprocess output as UTF-8 explicitly: Windows'
default `subprocess.run(text=True)` encoding is the OS locale (e.g. cp1252), which silently garbles
non-ASCII text the CLI prints as UTF-8 (no LLM call; the CLI invocation is mocked)."""

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
