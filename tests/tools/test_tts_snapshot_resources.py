from contextlib import contextmanager
from pathlib import Path

from tools import tts_tool


def test_neutts_uses_verified_helper_and_resource_fds(monkeypatch, tmp_path):
    import hermes_cli.kanban_runtime_snapshot as snapshot

    @contextmanager
    def resource(relative):
        values = {
            "tools/neutts_samples/jo.wav": tmp_path / "audio-fd",
            "tools/neutts_samples/jo.txt": tmp_path / "text-fd",
        }
        yield snapshot.SealedResourceFile(str(values[relative]), (10 if relative.endswith("wav") else 11,))

    captured = {}

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(snapshot, "snapshot_bootstrap_capability", lambda: object())
    monkeypatch.setattr(snapshot, "sealed_python_argv", lambda _p: ("/python", ["-c", "verified-synth"]))
    monkeypatch.setattr(snapshot, "sealed_resource_file", resource)
    monkeypatch.setattr(tts_tool.subprocess, "run", lambda cmd, **kwargs: captured.update(cmd=cmd, kwargs=kwargs) or Result())

    output = tmp_path / "out.wav"
    assert tts_tool._generate_neutts("hello", str(output), {}) == str(output)
    assert captured["cmd"][:3] == ["/python", "-c", "verified-synth"]
    assert captured["kwargs"]["pass_fds"] == (10, 11)
    assert str(tmp_path / "audio-fd") in captured["cmd"]
    assert str(tmp_path / "text-fd") in captured["cmd"]