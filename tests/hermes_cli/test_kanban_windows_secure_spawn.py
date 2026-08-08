from __future__ import annotations

from dataclasses import dataclass

import pytest


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
