from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_runtime_snapshot import (
    build_runtime_snapshot,
    prepare_snapshot_lease,
)



def _running_reviewer(conn, workspace: Path):
    task_id = kb.create_task(
        conn,
        title="review authority",
        assignee="critic",
        workspace_kind="dir",
        workspace_path=str(workspace),
        workflow_template_id="jerome-kanban-v1",
        current_step_key="critic",
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    task = kb.claim_task(conn, task_id, claimer="dispatcher:1", ttl_seconds=300)
    assert task is not None
    return task


def _assert_process_group_gone(pid: int) -> None:
    deadline = time.monotonic() + 2
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"reviewer process group {pid} survived handshake failure")
        time.sleep(0.01)


def _partial_handshake_timeout(
    monkeypatch,
    tmp_path: Path,
    *,
    phase: str,
):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = tmp_path / "payload.py"
    payload.write_text("pass\n")
    marker = tmp_path / "reviewer.pid"
    opened: list[int] = []
    processes: list[subprocess.Popen] = []
    waits: list[int] = []
    real_pipe = kb._posix_pipe
    real_popen = kb.subprocess.Popen
    real_wait = subprocess.Popen.wait

    def tracked_pipe():
        pair = real_pipe()
        opened.extend(pair)
        return pair

    def tracked_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        return proc

    def tracked_wait(self, *args, **kwargs):
        waits.append(self.pid)
        return real_wait(self, *args, **kwargs)

    monkeypatch.setattr(kb, "_posix_pipe", tracked_pipe)
    monkeypatch.setattr(subprocess.Popen, "wait", tracked_wait)
    monkeypatch.setattr(kb.subprocess, "Popen", tracked_popen)
    # Leave enough startup budget under parallel-suite CPU contention. The
    # injected child sleeps for one second after its partial frame, so 0.5s
    # still deterministically exercises timeout below the asserted 2s cap.
    monkeypatch.setattr(kb, "_REVIEWER_HANDSHAKE_TIMEOUT", 0.5)
    env = {
        "HERMES_REVIEWER_BOOTSTRAP_PARTIAL_PHASE": phase,
        "HERMES_REVIEWER_BOOTSTRAP_PID_MARKER": str(marker),
    }
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        started = time.monotonic()
        try:
            with pytest.raises(
                kb.ReviewerActivationStartFailed
                if phase.startswith("ready")
                else kb.ReviewerAuthorityError,
                match="activation_start_failed"
                if phase.startswith("ready")
                else "bootstrap_handshake_timeout",
            ):
                proc = kb._spawn_posix_reviewer(
                    conn,
                    task,
                    ["python", "-p", "critic", "__run_path__", str(payload)],
                    cwd=str(workspace),
                    env=env,
                    stdout=subprocess.DEVNULL,
                )
                proc.wait(timeout=2)
        finally:
            for proc in processes:
                if proc.poll() is None:
                    os.killpg(proc.pid, 9)
                    proc.wait(timeout=2)
        elapsed = time.monotonic() - started
        task_row = conn.execute(
            "SELECT status, worker_pid, current_run_id FROM tasks WHERE id=?", (task.id,)
        ).fetchone()
        run_row = conn.execute(
            "SELECT status, outcome, worker_pid FROM task_runs WHERE id=?",
            (task.current_run_id,),
        ).fetchone()

    assert elapsed < 2
    assert len(processes) == 1
    pid = processes[0].pid
    assert waits == [pid]
    assert marker.read_text(encoding="ascii") == str(pid)
    assert processes[0].poll() is not None
    _assert_process_group_gone(pid)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    if phase.startswith("ready"):
        assert task_row["status"] == "blocked"
        assert task_row["worker_pid"] == pid
        assert task_row["current_run_id"] is None
        assert run_row["status"] == "activation_start_failed"
        assert run_row["outcome"] == "activation_start_failed"
        assert run_row["worker_pid"] == pid
    else:
        assert task_row["status"] == "running"
        assert task_row["worker_pid"] is None
        assert task_row["current_run_id"] == task.current_run_id
        assert run_row["status"] == "running"
        assert run_row["worker_pid"] is None


@pytest.mark.parametrize("phase", ["hello_header", "hello_payload"])
def test_partial_hello_frame_times_out_before_pid_commit(monkeypatch, tmp_path, phase):
    _partial_handshake_timeout(monkeypatch, tmp_path, phase=phase)


@pytest.mark.parametrize("phase", ["ready_header", "ready_payload", "ready_eof"])
def test_partial_ready_frame_times_out_terminally_after_pid_commit(monkeypatch, tmp_path, phase):
    _partial_handshake_timeout(monkeypatch, tmp_path, phase=phase)


def test_reviewer_pid_grant_commits_all_pid_bindings_on_existing_connection(tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        grant = kb._commit_reviewer_authority(conn, task, 4321, parent_pid=os.getpid())

        task_row = conn.execute("SELECT worker_pid FROM tasks WHERE id=?", (task.id,)).fetchone()
        run_row = conn.execute(
            "SELECT worker_pid FROM task_runs WHERE id=?", (task.current_run_id,)
        ).fetchone()
        assert task_row["worker_pid"] == 4321
        assert run_row["worker_pid"] == 4321
        assert grant["pid"] == 4321
        assert grant["parent_pid"] == os.getpid()
        assert grant["role"] == "critic"


def test_reviewer_pid_grant_cas_loser_gets_no_grant(tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        kb._commit_reviewer_authority(conn, task, 4321, parent_pid=os.getpid())

        with pytest.raises(kb.ReviewerAuthorityError, match="worker_pid_cas_failed"):
            kb._commit_reviewer_authority(conn, task, 4322, parent_pid=os.getpid())


def test_reviewer_authority_uses_original_memory_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(kb.SCHEMA_SQL)
        workspace = Path.cwd()
        task = _running_reviewer(conn, workspace)
        grant = kb._commit_reviewer_authority(conn, task, 4321, parent_pid=os.getpid())
        assert grant["task_id"] == task.id
    finally:
        conn.close()


def test_non_reviewer_never_receives_reviewer_authority(tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(conn, title="generic", assignee="critic")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        task = kb.claim_task(conn, task_id, claimer="dispatcher:1", ttl_seconds=300)
        assert task is not None

        with pytest.raises(kb.ReviewerAuthorityError, match="reviewer_authority_not_applicable"):
            kb._commit_reviewer_authority(conn, task, 4321, parent_pid=os.getpid())


def test_commit_failure_never_writes_grant(tmp_path):
    class CommitFailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

        def rollback(self):
            return self.connection.rollback()

    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        failing = CommitFailingConnection(conn)
        with pytest.raises(kb.ReviewerAuthorityError, match="sqlite_authority_commit_failed"):
            kb._commit_reviewer_authority(failing, task, 4321, parent_pid=os.getpid())
        assert conn.execute("SELECT worker_pid FROM tasks WHERE id=?", (task.id,)).fetchone()[0] is None


def _sealed_reviewer_runtime(tmp_path: Path):
    source = Path(__file__).resolve().parents[2]
    snapshot = build_runtime_snapshot(
        source,
        repository_id="test-repository",
        source_revision="a" * 40,
        source_dirty=True,
        cache_root=tmp_path / "snapshot-cache",
    )
    from hermes_cli.kanban_runtime_snapshot import (
        runtime_binding_for_directory,
        runtime_binding_pass_fds,
        with_runtime_bindings,
    )

    clean_stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    runtime_bindings = [runtime_binding_for_directory("stdlib", clean_stdlib)]
    dynload = clean_stdlib / "lib-dynload"
    if dynload.is_dir():
        runtime_bindings.append(runtime_binding_for_directory("stdlib", dynload))
    snapshot = with_runtime_bindings(snapshot, tuple(runtime_bindings))
    lease = prepare_snapshot_lease(
        snapshot,
        cache_root=tmp_path / "snapshot-cache",
        task_id="t_review",
        run_id=1,
    )
    return kb.WorkerRuntimeSpec(
        argv=[
            sys.executable, "-I", "-S", "-c", kb._IMMUTABLE_RUNTIME_LAUNCHER,
            str(snapshot.object_root), snapshot.manifest_sha256, snapshot.cache_key,
        ],
        snapshot=snapshot,
        lease=lease,
        pass_fds=runtime_binding_pass_fds(snapshot.runtime_bindings),
    )


def test_real_spawn_binds_sealed_snapshot_lease_after_ready_eof(tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "runtime-origin.json"
    payload = tmp_path / "payload.py"
    payload.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "import tools.reviewer_authority as authority\n"
        "import hermes_cli.kanban_runtime_snapshot as snapshot_module\n"
        "from hermes_cli.kanban_runtime_snapshot import snapshot_bootstrap_capability\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{"
        "'sys_path': sys.path, 'authority': authority.__file__, "
        "'snapshot_module': snapshot_module.__file__, "
        "'manifest': snapshot_bootstrap_capability().spec.manifest_sha256"
        "}))\n"
    )
    runtime = _sealed_reviewer_runtime(tmp_path)
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        proc = kb._spawn_posix_reviewer(
            conn,
            task,
            [*runtime.argv, "-p", "critic", "__run_path__", str(payload)],
            cwd=str(workspace),
            env={},
            stdout=subprocess.PIPE,
            runtime=runtime,
        )
        output, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0, output.decode(errors="replace")

    record = json.loads(runtime.lease.path.read_text(encoding="utf-8"))
    origin = json.loads(marker.read_text(encoding="utf-8"))
    sealed_root = str(runtime.snapshot.payload_root)
    assert record["state"] == "bound"
    assert record["pid"] == proc.pid
    assert origin["manifest"] == runtime.snapshot.manifest_sha256
    assert origin["sys_path"][0] == sealed_root
    assert origin["authority"].startswith(sealed_root)
    assert origin["snapshot_module"].startswith(sealed_root)


def test_typed_bootstrap_uses_verified_snapshot_module_after_path_swap(monkeypatch, tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = tmp_path / "payload.py"
    marker = tmp_path / "attacker-ran"
    payload.write_text("pass\n")

    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        runtime = _sealed_reviewer_runtime(tmp_path)
        assert runtime.snapshot is not None
        original = kb.subprocess.Popen

        def swap_before_spawn(argv, **kwargs):
            target = runtime.snapshot.payload_root / "hermes_cli/kanban_runtime_snapshot.py"
            target.chmod(0o600)
            target.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            return original(argv, **kwargs)

        monkeypatch.setattr(kb.subprocess, "Popen", swap_before_spawn)
        child_log = tmp_path / "child.log"
        with child_log.open("wb") as output:
            try:
                proc = kb._spawn_posix_reviewer(
                    conn,
                    task,
                    [sys.executable, "-p", "critic", "__run_path__", str(payload)],
                    cwd=str(workspace),
                    env={},
                    stdout=output,
                    runtime=runtime,
                )
            except Exception:
                output.flush()
                pytest.fail(child_log.read_text(errors="replace"))
        assert proc.wait(timeout=10) == 0
        assert not marker.exists()


def test_real_spawn_uses_ready_eof_before_return_and_commits_pid(tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = tmp_path / "payload.py"
    payload.write_text("pass\n")
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        proc = kb._spawn_posix_reviewer(
            conn,
            task,
            ["python", "-p", "critic", "__run_path__", str(payload)],
            cwd=str(workspace),
            env={},
            stdout=subprocess.DEVNULL,
        )
        assert conn.execute("SELECT worker_pid FROM tasks WHERE id=?", (task.id,)).fetchone()[0] == proc.pid
        assert proc.wait(timeout=10) == 0


def test_generic_real_spawn_cannot_run_payload_before_lease_bound_and_release(monkeypatch, tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "generic-ran"
    payload = tmp_path / "payload.py"
    payload.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")

    with kb.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(conn, title="generic", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        task = kb.claim_task(conn, task_id, claimer="dispatcher:1", ttl_seconds=300)
        assert task is not None
        runtime = _sealed_reviewer_runtime(tmp_path)
        assert runtime.lease is not None
        original_commit = kb._commit_generic_worker_binding
        original_bind = __import__(
            "hermes_cli.kanban_runtime_snapshot", fromlist=["bind_snapshot_lease"]
        ).bind_snapshot_lease

        def commit_after_observation(conn_arg, task_arg, pid, *, parent_pid):
            assert not marker.exists()
            return original_commit(conn_arg, task_arg, pid, parent_pid=parent_pid)

        def bind_after_hold(lease, *, pid):
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.01)
            assert not marker.exists(), "generic payload ran before lease bound/release"
            return original_bind(lease, pid=pid)

        monkeypatch.setattr(kb, "_commit_generic_worker_binding", commit_after_observation)
        monkeypatch.setattr(
            "hermes_cli.kanban_runtime_snapshot.bind_snapshot_lease", bind_after_hold
        )
        proc = kb._spawn_posix_generic_worker(
            conn,
            task,
            [sys.executable, "-p", "default", "__run_path__", str(payload)],
            cwd=str(workspace),
            env={},
            stdout=subprocess.DEVNULL,
            runtime=runtime,
        )
        assert conn.execute("SELECT worker_pid FROM tasks WHERE id=?", (task.id,)).fetchone()[0] == proc.pid
        assert proc.wait(timeout=10) == 0
        assert marker.read_text() == "ran"
        assert json.loads(runtime.lease.path.read_text())["state"] == "bound"


def test_windows_reviewer_spawn_fails_closed_before_child_start(monkeypatch, tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)

    def unexpected_popen(*_args, **_kwargs):
        pytest.fail("unverified Windows reviewer bootstrap must not start a child")

    monkeypatch.setattr(kb.subprocess, "Popen", unexpected_popen)
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        with pytest.raises(kb.ReviewerAuthorityError, match="secure_job_unavailable"):
            kb._spawn_posix_reviewer(
                conn,
                task,
                ["python", "-p", "critic", "__run_path__", "payload.py"],
                cwd=str(workspace),
                env={},
                stdout=subprocess.DEVNULL,
            )


def test_popen_failure_closes_all_four_bootstrap_descriptors_once(monkeypatch, tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    opened = []
    closed = []
    real_pipe = kb._posix_pipe
    real_close = kb.os.close

    def tracked_pipe():
        pair = real_pipe()
        opened.extend(pair)
        return pair

    def tracked_close(fd):
        if fd in opened:
            closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(kb, "_posix_pipe", tracked_pipe)
    monkeypatch.setattr(kb.os, "close", tracked_close)
    monkeypatch.setattr(kb.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        with pytest.raises(OSError, match="boom"):
            kb._spawn_posix_reviewer(
                conn, task, ["python", "-p", "critic", "payload.py"],
                cwd=str(workspace), env={}, stdout=subprocess.DEVNULL,
            )

    assert len(opened) == 4
    assert sorted(closed) == sorted(opened)
    assert len(closed) == len(set(closed))


@pytest.mark.parametrize(
    "cmd",
    [
        ["python", "critic", "payload.py"],
        ["python", "-p"],
        ["python", "-p", "critic"],
        ["python", "-p", "planner", "payload.py"],
    ],
)
def test_malformed_reviewer_argv_fails_before_pipe_or_popen(monkeypatch, tmp_path, cmd):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pipe_calls = []
    popen_calls = []

    monkeypatch.setattr(kb, "_posix_pipe", lambda: pipe_calls.append(True))
    monkeypatch.setattr(kb.subprocess, "Popen", lambda *_a, **_k: popen_calls.append(True))
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        with pytest.raises(kb.ReviewerAuthorityError, match="bootstrap_protocol_violation"):
            kb._spawn_posix_reviewer(
                conn, task, cmd, cwd=str(workspace), env={}, stdout=subprocess.DEVNULL
            )

    assert pipe_calls == []
    assert popen_calls == []


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_pipe_fallback_set_inheritable_failure_closes_new_descriptors_once(
    monkeypatch, tmp_path, fail_on_call
):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    opened = []
    closed = []
    set_calls = 0
    popen_calls = []
    real_pipe = kb.os.pipe
    real_close = kb.os.close
    real_set_inheritable = kb.os.set_inheritable

    def tracked_pipe():
        pair = real_pipe()
        opened.extend(pair)
        return pair

    def failing_set_inheritable(fd, inheritable):
        nonlocal set_calls
        set_calls += 1
        if set_calls == fail_on_call:
            raise OSError(f"set inheritable failed {fail_on_call}")
        return real_set_inheritable(fd, inheritable)

    def tracked_close(fd):
        if fd in opened:
            closed.append(fd)
        return real_close(fd)

    monkeypatch.delattr(kb.os, "pipe2", raising=False)
    monkeypatch.setattr(kb.os, "pipe", tracked_pipe)
    monkeypatch.setattr(kb.os, "set_inheritable", failing_set_inheritable)
    monkeypatch.setattr(kb.os, "close", tracked_close)
    monkeypatch.setattr(
        kb.subprocess,
        "Popen",
        lambda *_a, **_k: popen_calls.append(True),
    )

    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        with pytest.raises(OSError, match=f"set inheritable failed {fail_on_call}"):
            kb._spawn_posix_reviewer(
                conn,
                task,
                ["python", "-p", "critic", "payload.py"],
                cwd=str(workspace),
                env={},
                stdout=subprocess.DEVNULL,
            )

    assert len(opened) == 2
    assert closed == opened
    assert len(closed) == len(set(closed))
    assert popen_calls == []
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_second_pipe_failure_closes_first_pipe_descriptors_once(monkeypatch, tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_pair = os.pipe()
    calls = 0
    closed = []
    real_close = kb.os.close

    def failing_second_pipe():
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_pair
        raise OSError("second pipe failed")

    def tracked_close(fd):
        if fd in first_pair:
            closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(kb, "_posix_pipe", failing_second_pipe)
    monkeypatch.setattr(kb.os, "close", tracked_close)
    monkeypatch.setattr(
        kb.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("Popen must not run after pipe failure"),
    )
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        with pytest.raises(OSError, match="second pipe failed"):
            kb._spawn_posix_reviewer(
                conn,
                task,
                ["python", "-p", "critic", "payload.py"],
                cwd=str(workspace),
                env={},
                stdout=subprocess.DEVNULL,
            )

    assert sorted(closed) == sorted(first_pair)
    assert len(closed) == len(set(closed))


def test_saturated_grant_pipe_times_out_terminally_and_reaps_once(monkeypatch, tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = tmp_path / "payload.py"
    payload.write_text("pass\n")
    real_pipe = kb._posix_pipe
    pipe_calls = 0
    processes = []
    waits = []
    real_popen = kb.subprocess.Popen
    real_wait = subprocess.Popen.wait

    def saturated_parent_pipe():
        nonlocal pipe_calls
        pipe_calls += 1
        pair = real_pipe()
        if pipe_calls == 2:
            read_fd, write_fd = pair
            os.set_blocking(write_fd, False)
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(write_fd, True)
        return pair

    def tracked_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        return proc

    def tracked_wait(self, *args, **kwargs):
        waits.append(self.pid)
        return real_wait(self, *args, **kwargs)

    monkeypatch.setattr(kb, "_posix_pipe", saturated_parent_pipe)
    monkeypatch.setattr(subprocess.Popen, "wait", tracked_wait)
    monkeypatch.setattr(kb.subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(kb, "_REVIEWER_HANDSHAKE_TIMEOUT", 0.2)
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        started = time.monotonic()
        with pytest.raises(kb.ReviewerActivationStartFailed, match="activation_start_failed"):
            kb._spawn_posix_reviewer(
                conn,
                task,
                ["python", "-p", "critic", "__run_path__", str(payload)],
                cwd=str(workspace),
                env={"HERMES_REVIEWER_BOOTSTRAP_HANG_AFTER_HELLO": "1"},
                stdout=subprocess.DEVNULL,
            )
        elapsed = time.monotonic() - started
        task_row = conn.execute(
            "SELECT status, worker_pid, current_run_id FROM tasks WHERE id=?", (task.id,)
        ).fetchone()
        run_row = conn.execute(
            "SELECT status, outcome, worker_pid FROM task_runs WHERE id=?",
            (task.current_run_id,),
        ).fetchone()

    assert elapsed < 2
    assert len(processes) == 1
    pid = processes[0].pid
    assert waits == [pid]
    assert processes[0].poll() is not None
    _assert_process_group_gone(pid)
    assert task_row["status"] == "blocked"
    assert task_row["worker_pid"] == pid
    assert task_row["current_run_id"] is None
    assert run_row["status"] == "activation_start_failed"
    assert run_row["outcome"] == "activation_start_failed"
    assert run_row["worker_pid"] == pid


def test_post_commit_handshake_failure_is_terminal_and_keeps_pid_binding(monkeypatch, tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = tmp_path / "payload.py"
    payload.write_text("raise SystemExit(75)\n")
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        with pytest.raises(Exception):
            kb._spawn_posix_reviewer(
                conn, task,
                ["python", "-p", "critic", "__run_path__", str(payload)],
                cwd=str(workspace),
                env={"HERMES_REVIEWER_BOOTSTRAP_EXIT_AFTER_GRANT": "1"},
                stdout=subprocess.DEVNULL,
            )
        task_row = conn.execute(
            "SELECT status, worker_pid, current_run_id FROM tasks WHERE id=?", (task.id,)
        ).fetchone()
        run_row = conn.execute(
            "SELECT status, outcome, worker_pid FROM task_runs WHERE id=?", (task.current_run_id,)
        ).fetchone()
        events = [row[0] for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task.id,)
        )]

    assert task_row["status"] == "blocked"
    assert task_row["worker_pid"] is not None
    assert task_row["current_run_id"] is None
    assert run_row["status"] == "activation_start_failed"
    assert run_row["outcome"] == "activation_start_failed"
    assert run_row["worker_pid"] == task_row["worker_pid"]
    assert events.count("activation_start_failed") == 1
    assert "spawn_failed" not in events


class _FakeHandshakeProcess:
    def __init__(self, wait_results, poll_results):
        self.pid = 4242
        self._wait_results = iter(wait_results)
        self._poll_results = iter(poll_results)
        self.wait_calls = []

    def poll(self):
        return next(self._poll_results)

    def wait(self, timeout):
        self.wait_calls.append(timeout)
        result = next(self._wait_results)
        if isinstance(result, BaseException):
            raise result
        return result


def test_abort_and_reap_waits_after_sigterm_without_sigkill_when_child_exits(monkeypatch):
    proc = _FakeHandshakeProcess([0], [None, 0, 0])
    signals = []
    monkeypatch.setattr(kb, "_IS_WINDOWS", False)
    monkeypatch.setattr(kb.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    owner = kb.HandshakeOwner(proc, ())
    owner.abort_and_reap()

    assert signals == [(proc.pid, kb.signal.SIGTERM)]
    assert proc.wait_calls == [2]
    assert owner.state == "REAPED"


def test_abort_and_reap_escalates_only_after_sigterm_wait_times_out(monkeypatch):
    proc = _FakeHandshakeProcess([0], [None, None, None])
    events = []
    monkeypatch.setattr(kb, "_IS_WINDOWS", False)
    monkeypatch.setattr(kb, "_REVIEWER_TERMINATE_GRACE", 0)
    monkeypatch.setattr(
        kb.os, "killpg", lambda pid, sig: events.append(("signal", pid, sig))
    )

    def tracked_wait(timeout):
        events.append(("wait", timeout))
        return _FakeHandshakeProcess.wait(proc, timeout)

    monkeypatch.setattr(proc, "wait", tracked_wait)
    owner = kb.HandshakeOwner(proc, ())
    owner.abort_and_reap()

    assert events == [
        ("signal", proc.pid, kb.signal.SIGTERM),
        ("signal", proc.pid, kb.signal.SIGKILL),
        ("wait", 2),
    ]
    assert proc.wait_calls == [2]
    assert owner.state == "REAPED"


def test_handshake_timeout_uses_one_deadline_and_reaps_once(monkeypatch, tmp_path):
    db_path = tmp_path / "authority.db"
    kb.init_db(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    waits = []
    real_wait = subprocess.Popen.wait

    def counted_wait(self, *args, **kwargs):
        waits.append(self.pid)
        return real_wait(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "wait", counted_wait)
    monkeypatch.setattr(kb, "_REVIEWER_HANDSHAKE_TIMEOUT", 0.2)
    with kb.connect_closing(db_path=db_path) as conn:
        task = _running_reviewer(conn, workspace)
        started = time.monotonic()
        with pytest.raises(kb.ReviewerAuthorityError, match="bootstrap_handshake_timeout"):
            kb._spawn_posix_reviewer(
                conn, task,
                ["python", "-p", "critic", "__run_path__", str(tmp_path / "never.py")],
                cwd=str(workspace),
                env={"HERMES_REVIEWER_BOOTSTRAP_HANG_BEFORE_HELLO": "1"},
                stdout=subprocess.DEVNULL,
            )
        elapsed = time.monotonic() - started

    assert elapsed < 2
    assert len(waits) == 1