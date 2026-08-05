"""Gateway startup resource-limit hardening tests."""

import os
import subprocess
import sys
from types import ModuleType

import hermes_cli.gateway as gateway


class FakeResource:
    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def __init__(self, limits):
        self.limits = limits
        self.set_calls = []

    def getrlimit(self, resource_id):
        assert resource_id == self.RLIMIT_NOFILE
        return self.limits

    def setrlimit(self, resource_id, limits):
        assert resource_id == self.RLIMIT_NOFILE
        self.set_calls.append(limits)
        self.limits = limits


class DeniedResource(FakeResource):
    def setrlimit(self, resource_id, limits):
        raise PermissionError("denied")


def test_raise_gateway_nofile_soft_limit_from_256_to_4096():
    resource = FakeResource((256, 8192))

    result = gateway._raise_gateway_nofile_soft_limit(resource_module=resource)

    assert result == (256, 4096)
    assert resource.set_calls == [(4096, 8192)]


def test_raise_gateway_nofile_soft_limit_clamps_to_finite_hard_limit():
    resource = FakeResource((256, 1024))

    result = gateway._raise_gateway_nofile_soft_limit(resource_module=resource)

    assert result == (256, 1024)
    assert resource.set_calls == [(1024, 1024)]


def test_raise_gateway_nofile_soft_limit_never_lowers_existing_limit():
    resource = FakeResource((8192, 16384))

    result = gateway._raise_gateway_nofile_soft_limit(resource_module=resource)

    assert result == (8192, 8192)
    assert resource.set_calls == []


def test_raise_gateway_nofile_soft_limit_fails_open_when_setrlimit_is_denied():
    resource = DeniedResource((256, 8192))

    result = gateway._raise_gateway_nofile_soft_limit(resource_module=resource)

    assert result == (256, 256)


def test_run_gateway_raises_nofile_before_gateway_runtime_import(monkeypatch):
    events = []

    monkeypatch.setattr(gateway, "_guard_official_docker_root_gateway", lambda: None)
    monkeypatch.setattr(
        gateway, "_guard_named_profile_under_multiplexer", lambda force=False: None
    )
    monkeypatch.setattr(
        gateway, "_guard_supervised_gateway_conflict", lambda force=False: None
    )
    monkeypatch.setattr(
        gateway,
        "_guard_existing_gateway_process_conflict",
        lambda replace=False: None,
    )
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(
        gateway,
        "_raise_gateway_nofile_soft_limit",
        lambda: events.append("raise-limit") or (256, 4096),
    )

    async def start_gateway(*, replace, verbosity):
        events.append("start-gateway")
        return False

    fake_run = ModuleType("gateway.run")
    setattr(fake_run, "start_gateway", start_gateway)

    def exit_after_graceful_shutdown(code):
        raise SystemExit(code)

    setattr(fake_run, "_exit_after_graceful_shutdown", exit_after_graceful_shutdown)
    monkeypatch.setitem(sys.modules, "gateway.run", fake_run)

    try:
        gateway.run_gateway()
    except SystemExit:
        pass

    assert events[:2] == ["raise-limit", "start-gateway"]


def test_real_child_process_raises_soft_limit_from_256_to_4096():
    if os.name != "posix":
        import pytest

        pytest.skip("POSIX resource limits only")

    script = """
import resource
from hermes_cli.gateway import _raise_gateway_nofile_soft_limit
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])
print(_raise_gateway_nofile_soft_limit())
print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.fspath(gateway.PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["256", "(256, 4096)", "4096"]
