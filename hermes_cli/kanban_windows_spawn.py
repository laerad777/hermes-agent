"""Windows Job Object process spawning for bounded Kanban subprocesses.

The public adapter remains fail-closed until it has run on trusted Windows CI.
The verified core is dependency-injected so its ordering and cleanup invariants
can be exercised on non-Windows development hosts without claiming runtime proof.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence


UNVERIFIED_PENDING_WINDOWS_CI = "UNVERIFIED_PENDING_WINDOWS_CI"
_ERROR_EXIT_CODE = 1


class WindowsSecureSpawnError(RuntimeError):
    """A secure Windows process could not be created or contained."""


@dataclass(frozen=True)
class WindowsSpawnSpec:
    argv: Sequence[str]
    cwd: Optional[str]
    env: Mapping[str, str]
    stdin_handle: int
    stdout_handle: int
    stderr_handle: int
    inherit_handles: Sequence[int]


@dataclass(frozen=True)
class _CreatedChild:
    pid: int
    process_handle: int
    thread_handle: int


class _WindowsApi(Protocol):
    def create_suspended(self, spec: WindowsSpawnSpec) -> _CreatedChild: ...
    def create_job(self) -> int: ...
    def set_kill_on_close(self, job: int) -> None: ...
    def assign_process_to_job(self, job: int, process: int) -> None: ...
    def resume_thread(self, thread: int) -> None: ...
    def terminate_job(self, job: int, code: int) -> None: ...
    def terminate_process(self, process: int, code: int) -> None: ...
    def wait_process(self, process: int, timeout_ms: int) -> int: ...
    def get_exit_code(self, process: int) -> int: ...
    def close_handle(self, handle: int) -> None: ...


class WindowsJobProcess:
    """Own a process and its kill-on-close Job Object handles exactly once."""

    def __init__(self, child: _CreatedChild, job_handle: int, api: _WindowsApi):
        self.pid = child.pid
        self._process_handle: Optional[int] = child.process_handle
        self._job_handle: Optional[int] = job_handle
        self._api = api
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        if self.returncode is not None:
            return self.returncode
        process = self._process_handle
        if process is None:
            return self.returncode
        if self._api.wait_process(process, 0) != 0:
            return None
        self.returncode = int(self._api.get_exit_code(process))
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        process = self._process_handle
        if process is None:
            return int(self.returncode or 0)
        timeout_ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        if self._api.wait_process(process, timeout_ms) != 0:
            raise subprocess.TimeoutExpired(tuple(), timeout)
        self.returncode = int(self._api.get_exit_code(process))
        return self.returncode

    def terminate_tree(self, *, exit_code: int = _ERROR_EXIT_CODE) -> None:
        if self._job_handle is not None:
            self._api.terminate_job(self._job_handle, int(exit_code))

    def terminate(self) -> None:
        self.terminate_tree()

    def kill(self) -> None:
        self.terminate_tree()

    def close(self) -> None:
        process, job = self._process_handle, self._job_handle
        self._process_handle = None
        self._job_handle = None
        if process is not None:
            self._api.close_handle(process)
        if job is not None:
            self._api.close_handle(job)


def _validate_spec(spec: WindowsSpawnSpec) -> None:
    if not spec.argv or not all(isinstance(value, str) and value for value in spec.argv):
        raise WindowsSecureSpawnError("invalid_argv")
    expected = {spec.stdin_handle, spec.stdout_handle, spec.stderr_handle}
    actual = set(spec.inherit_handles)
    if actual != expected or len(tuple(spec.inherit_handles)) != len(actual):
        raise WindowsSecureSpawnError("handle_list_mismatch")
    if any(not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0 for handle in actual):
        raise WindowsSecureSpawnError("handle_list_mismatch")


def _spawn_windows_secure_verified(
    spec: WindowsSpawnSpec, *, api: _WindowsApi,
) -> WindowsJobProcess:
    """Create suspended, contain, then resume; fail closed at every boundary."""
    _validate_spec(spec)
    child: Optional[_CreatedChild] = None
    job: Optional[int] = None
    thread_closed = False
    try:
        child = api.create_suspended(spec)
        job = api.create_job()
        api.set_kill_on_close(job)
        api.assign_process_to_job(job, child.process_handle)
        api.resume_thread(child.thread_handle)
        api.close_handle(child.thread_handle)
        thread_closed = True
        return WindowsJobProcess(child, job, api)
    except BaseException as exc:
        if child is not None:
            try:
                if job is not None:
                    api.terminate_job(job, _ERROR_EXIT_CODE)
                else:
                    api.terminate_process(child.process_handle, _ERROR_EXIT_CODE)
            except OSError:
                pass
            if not thread_closed:
                api.close_handle(child.thread_handle)
            api.close_handle(child.process_handle)
        if job is not None:
            api.close_handle(job)
        if isinstance(exc, WindowsSecureSpawnError):
            raise
        raise WindowsSecureSpawnError(f"secure_spawn_failed: {exc}") from exc


def spawn_windows_secure(
    spec: WindowsSpawnSpec, *, api: Optional[_WindowsApi] = None,
) -> WindowsJobProcess:
    """Fail before child creation until the native adapter passes Windows CI."""
    del spec, api
    raise WindowsSecureSpawnError(UNVERIFIED_PENDING_WINDOWS_CI)


class NativeWindowsApi:
    """Native STARTUPINFOEX/Job Object API, importable only on Windows."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _WAIT_OBJECT_0 = 0

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsSecureSpawnError(UNVERIFIED_PENDING_WINDOWS_CI)
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _checked(self, result, name: str):
        if not result:
            raise OSError(self.ctypes.get_last_error(), name)
        return result

    def create_suspended(self, spec: WindowsSpawnSpec) -> _CreatedChild:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
        startup.hStdInput = spec.stdin_handle
        startup.hStdOutput = spec.stdout_handle
        startup.hStdError = spec.stderr_handle
        startup.lpAttributeList = {"handle_list": list(spec.inherit_handles)}
        proc = subprocess.Popen(
            list(spec.argv), cwd=spec.cwd, env=dict(spec.env),
            stdin=None, stdout=None, stderr=None, close_fds=True,
            startupinfo=startup,
            creationflags=subprocess.CREATE_SUSPENDED
            | getattr(subprocess, "EXTENDED_STARTUPINFO_PRESENT", 0x00080000)
            | subprocess.CREATE_NO_WINDOW,
        )
        process_handle = int(proc._handle)  # native Popen contract on Windows
        thread_handle = int(getattr(proc, "_thread", 0))
        if not thread_handle:
            proc.kill()
            proc.wait()
            raise WindowsSecureSpawnError("thread_handle_unavailable")
        return _CreatedChild(proc.pid, process_handle, thread_handle)

    def create_job(self) -> int:
        return int(self._checked(self.kernel32.CreateJobObjectW(None, None), "CreateJobObjectW"))

    def set_kill_on_close(self, job: int) -> None:
        class _BASIC_LIMITS(self.ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", self.ctypes.c_int64),
                ("PerJobUserTimeLimit", self.ctypes.c_int64),
                ("LimitFlags", self.wintypes.DWORD),
                ("MinimumWorkingSetSize", self.ctypes.c_size_t),
                ("MaximumWorkingSetSize", self.ctypes.c_size_t),
                ("ActiveProcessLimit", self.wintypes.DWORD),
                ("Affinity", self.ctypes.c_size_t),
                ("PriorityClass", self.wintypes.DWORD),
                ("SchedulingClass", self.wintypes.DWORD),
            ]

        class _IO_COUNTERS(self.ctypes.Structure):
            _fields_ = [(name, self.ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _EXTENDED(self.ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMITS),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", self.ctypes.c_size_t),
                ("JobMemoryLimit", self.ctypes.c_size_t),
                ("PeakProcessMemoryUsed", self.ctypes.c_size_t),
                ("PeakJobMemoryUsed", self.ctypes.c_size_t),
            ]

        limits = _EXTENDED()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self._checked(
            self.kernel32.SetInformationJobObject(
                job, self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                self.ctypes.byref(limits), self.ctypes.sizeof(limits),
            ),
            "SetInformationJobObject",
        )

    def assign_process_to_job(self, job: int, process: int) -> None:
        self._checked(self.kernel32.AssignProcessToJobObject(job, process), "AssignProcessToJobObject")

    def resume_thread(self, thread: int) -> None:
        if self.kernel32.ResumeThread(thread) == 0xFFFFFFFF:
            raise OSError(self.ctypes.get_last_error(), "ResumeThread")

    def terminate_job(self, job: int, code: int) -> None:
        self._checked(self.kernel32.TerminateJobObject(job, code), "TerminateJobObject")

    def terminate_process(self, process: int, code: int) -> None:
        self._checked(self.kernel32.TerminateProcess(process, code), "TerminateProcess")

    def wait_process(self, process: int, timeout_ms: int) -> int:
        return 0 if self.kernel32.WaitForSingleObject(process, timeout_ms) == self._WAIT_OBJECT_0 else 1

    def get_exit_code(self, process: int) -> int:
        value = self.wintypes.DWORD()
        self._checked(self.kernel32.GetExitCodeProcess(process, self.ctypes.byref(value)), "GetExitCodeProcess")
        return int(value.value)

    def close_handle(self, handle: int) -> None:
        self._checked(self.kernel32.CloseHandle(handle), "CloseHandle")
