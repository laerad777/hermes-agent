"""Contract tests for the direct POSIX stdio MCP child watchdog."""

import os
import subprocess
import sys

import pytest

from tools import mcp_stdio_watchdog, mcp_tool


def test_is_orphaned_is_false_while_direct_parent_is_unchanged():
    original_ppid = 1234

    assert mcp_stdio_watchdog._is_orphaned(
        original_ppid,
        getppid=lambda: original_ppid,
    ) is False


@pytest.mark.skipif(os.name != "posix", reason="watchdog wrapping is POSIX-only")
def test_wrap_command_uses_stable_parent_pid_and_preserves_command_tail():
    parent_pid = os.getpid()
    command = "/opt/hermes/bin/mcp-server"
    command_args = ["--label", "value with spaces", "--", "literal-tail"]

    wrapped_command, wrapped_args = mcp_tool._wrap_command_with_watchdog(
        command,
        command_args,
    )

    assert wrapped_command == sys.executable
    assert wrapped_args == [
        os.path.join(os.path.dirname(mcp_tool.__file__), "mcp_stdio_watchdog.py"),
        "--ppid",
        str(parent_pid),
        "--",
        command,
        *command_args,
    ]
    assert "--create-time" not in wrapped_args


def test_snapshot_watchdog_uses_verified_python_bytes(monkeypatch):
    import hermes_cli.kanban_runtime_snapshot as snapshot

    monkeypatch.setattr(snapshot, "snapshot_bootstrap_capability", lambda: object())
    monkeypatch.setattr(
        snapshot,
        "sealed_python_argv",
        lambda relative: ("/python", ["-c", "verified-watchdog"]),
    )

    command, args = mcp_tool._wrap_command_with_watchdog("server", ["--flag"])

    assert command == "/python"
    assert args[:2] == ["-c", "verified-watchdog"]
    assert args[2:] == ["--ppid", str(os.getpid()), "--", "server", "--flag"]


def test_snapshot_watchdog_executes_verified_bytes_after_payload_swap(monkeypatch, tmp_path):
    import hermes_cli.kanban_runtime_snapshot as snapshot

    source = tmp_path / "source"
    helper = source / "tools/mcp_stdio_watchdog.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("print('verified')\n")
    (source / "agent").mkdir()
    (source / "agent/i18n.py").write_text("SUPPORTED_LANGUAGES = {'en': 'English'}\n")
    (source / "locales").mkdir()
    (source / "locales/en.yaml").write_text("hello: Hello\n")
    spec = snapshot.build_runtime_snapshot(
        source, repository_id="repo", source_revision="a" * 40,
        source_dirty=True, cache_root=tmp_path / "cache",
    )
    snapshot.install_snapshot_bootstrap_capability(spec)
    target = spec.payload_root / "tools/mcp_stdio_watchdog.py"
    target.chmod(0o600)
    target.write_text("print('attacker')\n")

    command, args = mcp_tool._wrap_command_with_watchdog("server", [])
    completed = subprocess.run(
        [command, *args[:2]], capture_output=True, text=True, check=False,
    )
    assert completed.stdout.strip() == "verified"
