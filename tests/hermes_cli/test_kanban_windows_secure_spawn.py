from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


@dataclass
class _Child:
    pid: int = 4242
    process_handle: int = 11
    thread_handle: int = 12


class _FakeWindowsApi:
    def __init__(self, *, fail_at: str | None = None):
        self.fail_at = fail_at
        self.calls: list[tuple] = []
        self.child = _Child()

    def create_suspended(self, spec):
        self.calls.append(("create_suspended", tuple(spec.inherit_handles)))
        if self.fail_at == "create_suspended":
            raise OSError("create failed")
        return self.child

    def create_job(self):
        self.calls.append(("create_job",))
        if self.fail_at == "create_job":
            raise OSError("job failed")
        return 21

    def set_kill_on_close(self, job):
        self.calls.append(("set_kill_on_close", job))
        if self.fail_at == "set_kill_on_close":
            raise OSError("limits failed")

    def assign_process_to_job(self, job, process):
        self.calls.append(("assign", job, process))
        if self.fail_at == "assign":
            raise OSError("nested job denied")

    def resume_thread(self, thread):
        self.calls.append(("resume", thread))
        if self.fail_at == "resume":
            raise OSError("resume failed")

    def terminate_job(self, job, code):
        self.calls.append(("terminate_job", job, code))

    def terminate_process(self, process, code):
        self.calls.append(("terminate_process", process, code))

    def wait_process(self, process, timeout_ms):
        self.calls.append(("wait", process, timeout_ms))
        return 0

    def get_exit_code(self, process):
        self.calls.append(("exit_code", process))
        return 0

    def close_handle(self, handle):
        self.calls.append(("close", handle))


def _spec(module):
    return module.WindowsSpawnSpec(
        argv=("python.exe", "-c", "pass"),
        cwd="C:\\sealed",
        env={"SystemRoot": "C:\\Windows"},
        stdin_handle=31,
        stdout_handle=32,
        stderr_handle=32,
        inherit_handles=(31, 32),
    )


def test_windows_secure_spawn_is_disabled_before_any_child_creation():
    import hermes_cli.kanban_windows_spawn as module

    api = _FakeWindowsApi()

    with pytest.raises(module.WindowsSecureSpawnError, match="UNVERIFIED_PENDING_WINDOWS_CI"):
        module.spawn_windows_secure(_spec(module), api=api)

    assert api.calls == []


def test_verified_adapter_assigns_kill_on_close_job_before_resuming_and_closes_once():
    import hermes_cli.kanban_windows_spawn as module

    api = _FakeWindowsApi()
    process = module._spawn_windows_secure_verified(_spec(module), api=api)

    assert api.calls[:5] == [
        ("create_suspended", (31, 32)),
        ("create_job",),
        ("set_kill_on_close", 21),
        ("assign", 21, 11),
        ("resume", 12),
    ]
    assert api.calls.count(("close", 12)) == 1

    process.close()
    process.close()

    assert api.calls.count(("close", 11)) == 1
    assert api.calls.count(("close", 21)) == 1


@pytest.mark.parametrize("failure", ["create_job", "set_kill_on_close", "assign", "resume"])
def test_verified_adapter_failures_terminate_suspended_child_and_close_every_handle_once(failure):
    import hermes_cli.kanban_windows_spawn as module

    api = _FakeWindowsApi(fail_at=failure)

    with pytest.raises(module.WindowsSecureSpawnError):
        module._spawn_windows_secure_verified(_spec(module), api=api)

    if failure == "create_job":
        assert ("terminate_process", 11, 1) in api.calls
    else:
        assert ("terminate_job", 21, 1) in api.calls
    assert api.calls.count(("close", 12)) == 1
    assert api.calls.count(("close", 11)) == 1
    assert api.calls.count(("close", 21)) == (0 if failure == "create_job" else 1)


def test_timeout_terminates_job_not_taskkill_and_releases_handles():
    import hermes_cli.kanban_windows_spawn as module

    api = _FakeWindowsApi()
    process = module._spawn_windows_secure_verified(_spec(module), api=api)

    process.terminate_tree(exit_code=1460)
    process.close()

    assert ("terminate_job", 21, 1460) in api.calls
    assert api.calls.count(("close", 11)) == 1
    assert api.calls.count(("close", 21)) == 1
    assert all("taskkill" not in repr(call).lower() for call in api.calls)


def test_handle_list_must_exactly_cover_stdio_inheritance():
    import hermes_cli.kanban_windows_spawn as module

    spec = module.WindowsSpawnSpec(
        argv=("python.exe",), cwd=None, env={}, stdin_handle=31,
        stdout_handle=32, stderr_handle=33, inherit_handles=(31, 32),
    )

    with pytest.raises(module.WindowsSecureSpawnError, match="handle_list_mismatch"):
        module._spawn_windows_secure_verified(spec, api=_FakeWindowsApi())


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_default_spawn_preserves_generic_windows_worker_path(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return FakeProc()

    class FakeThread:
        def __init__(self, **kwargs):
            captured["thread"] = kwargs

        def start(self):
            captured["thread_started"] = True

    monkeypatch.setattr(kb, "_IS_WINDOWS", True)
    monkeypatch.setattr(kb, "_worker_runtime_spec", lambda task, workspace: SimpleNamespace(
        argv=("hermes",), env={}, lease=None, snapshot=None, pass_fds=()
    ))
    monkeypatch.setattr(kb.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb.threading, "Thread", FakeThread)
    monkeypatch.setattr(kb.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="generic", assignee="executor")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert kb._default_spawn(task, str(workspace), authority_conn=conn) == 4242

    assert captured["cmd"][-3:] == ["chat", "-q", f"work kanban task {task_id}"]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["start_new_session"] is False
    assert captured["thread_started"] is True


def test_default_spawn_rejects_typed_windows_reviewer_before_popen(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    popen_called = False

    def fake_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not run for typed Windows reviewers")

    monkeypatch.setattr(kb, "_IS_WINDOWS", True)
    monkeypatch.setattr(kb, "_worker_runtime_spec", lambda task, workspace: SimpleNamespace(
        argv=("hermes",), env={}, lease=None, snapshot=None, pass_fds=()
    ))
    monkeypatch.setattr(kb.subprocess, "Popen", fake_popen)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="critic",
            assignee="critic",
            workflow_template_id="jerome-kanban-v1",
            current_step_key="critic",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        with pytest.raises(kb.ReviewerAuthorityError, match="UNVERIFIED_PENDING_WINDOWS_CI"):
            kb._default_spawn(task, str(workspace), authority_conn=conn)

    assert popen_called is False
