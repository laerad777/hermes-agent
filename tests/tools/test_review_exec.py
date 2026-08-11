from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff --check",
        "git rev-parse HEAD",
        "shasum -a 256 pyproject.toml",
    ],
)
def test_review_exec_accepts_bounded_inspection_commands(command):
    from tools.review_exec_tool import validate_review_command

    assert validate_review_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "printf hacked > source.py",
        "python -c \"open('source.py', 'w').write('x')\"",
        "pip install rich",
        "npm install",
        "git commit -am hacked",
        "git merge main",
        "git push origin HEAD",
        "git checkout -- source.py",
        "git diff --output=patch.txt",
        "rm -rf .",
        "kill 1234",
        "hermes gateway restart",
        "hermes config set model x",
        "sqlite3 ~/.hermes/kanban.db 'delete from tasks'",
        "git -C .. status",
        "git branch",
        "git diff --no-index a b",
        "git diff --ext-diff",
        "git diff --textconv",
        "git -c core.pager=touch status",
        "git --config-env=x=y status",
        "git status --no-optional-locks",
        "python -m pytest",
        "pytest",
        "scripts/run_tests.sh tests/tools/test_review_exec.py -q",
        "npm test",
    ],
)
def test_review_exec_rejects_mutation_and_control_commands(command):
    from tools.review_exec_tool import ReviewCommandDenied, validate_review_command

    with pytest.raises(ReviewCommandDenied):
        validate_review_command(command)


def test_review_exec_runs_canary_in_workspace(monkeypatch, tmp_path):
    from tools.review_exec_tool import review_exec

    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    result = json.loads(review_exec("git rev-parse --show-toplevel", workdir=str(tmp_path)))

    assert result.get("success", False) is False
    assert result["exit_code"] != 0
    assert "command" not in result


def test_review_exec_ignores_path_executable_substitution(monkeypatch, tmp_path):
    from tools.review_exec_tool import review_exec

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "git").symlink_to("/usr/bin/touch")
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    result = json.loads(review_exec("git status", workdir=str(workspace)))

    assert not (workspace / "status").exists()
    assert result["success"] is False


def test_review_exec_rejects_outside_and_symlink_operands(monkeypatch, tmp_path):
    from tools.review_exec_tool import review_exec

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hosts-link").symlink_to("/etc/hosts")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    outside = json.loads(review_exec("shasum -a 256 /etc/hosts"))
    symlink = json.loads(review_exec("shasum -a 256 hosts-link"))

    assert outside.get("success", False) is False
    assert symlink.get("success", False) is False


def test_review_exec_uses_fail_closed_environment(monkeypatch, tmp_path):
    import tools.review_exec_tool as module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("PYTHONPATH", "/attacker")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    captured = {}

    class FakeProcess:
        pid = os.getpid()
        stdout = None

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    result = json.loads(module.review_exec("git status"))

    assert result["success"] is True
    assert "PYTHONPATH" not in captured["env"]
    assert "GIT_CONFIG_COUNT" not in captured["env"]
    assert captured["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert captured["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert captured["env"]["GIT_PAGER"] == "cat"


def test_review_exec_streams_only_the_bounded_output_tail(tmp_path):
    from tools.review_exec_tool import _MAX_OUTPUT_BYTES, _run_bounded

    code = "import sys; sys.stdout.buffer.write(b'a' * 120000)"
    returncode, output = _run_bounded(
        [sys.executable, "-c", code], cwd=tmp_path, timeout=5,
    )

    assert returncode == 0
    assert len(output.encode()) == _MAX_OUTPUT_BYTES


def test_review_exec_timeout_kills_descendants(tmp_path):
    from tools.review_exec_tool import _run_bounded

    pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded(
            [sys.executable, "-c", code, str(pid_file)], cwd=tmp_path, timeout=1,
        )

    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"descendant {child_pid} survived review_exec timeout")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_review_exec_timeout_kills_orphan_after_leader_exits(tmp_path):
    from tools.review_exec_tool import _run_bounded

    pid_file = tmp_path / "child.pid"
    marker = tmp_path / "survived"
    child_code = (
        "import pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid())); "
        "time.sleep(2); pathlib.Path(sys.argv[2]).write_text('survived'); time.sleep(30)"
    )
    parent_code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]], "
        "stdout=sys.stdout,stderr=sys.stderr)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded(
            [sys.executable, "-c", parent_code, child_code, str(pid_file), str(marker)],
            cwd=tmp_path, timeout=1,
        )

    child_pid = int(pid_file.read_text())
    time.sleep(2)
    assert not marker.exists()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"orphan descendant {child_pid} survived review_exec timeout")


def test_review_exec_windows_fails_closed_without_taskkill(monkeypatch, tmp_path):
    import tools.review_exec_tool as module

    monkeypatch.setattr(module, "_IS_WINDOWS", True)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))

    def unexpected_popen(*_args, **_kwargs):
        pytest.fail("unverified Windows review execution must not spawn a process")

    def unexpected_run(*_args, **_kwargs):
        pytest.fail("unverified Windows review execution must not use taskkill")

    monkeypatch.setattr(module.subprocess, "Popen", unexpected_popen)
    monkeypatch.setattr(module.subprocess, "run", unexpected_run)

    result = json.loads(module.review_exec("git status"))

    assert result.get("success", False) is False
    assert "UNVERIFIED_PENDING_WINDOWS_CI" in result["error"]


def test_review_exec_schema_is_exposed_without_mutation_tools(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")

    from model_tools import get_tool_definitions

    schemas = get_tool_definitions(
        ["review-readonly", "review-exec"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {schema["function"]["name"] for schema in schemas}

    assert "review_exec" in names
    assert not {"terminal", "process", "execute_code", "write_file", "patch"} & names