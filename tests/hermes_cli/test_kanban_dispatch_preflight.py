from __future__ import annotations

import json
import signal
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_board(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    review_skill = home / "skills" / "sdlc-review"
    review_skill.mkdir(parents=True)
    (review_skill / "SKILL.md").write_text(
        "---\nname: sdlc-review\ndescription: Review changes.\n---\n# Review\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board(slug="default", name="Test")
    with kb.connect_closing() as conn:
        yield conn, home


def _ready_task(conn, **kwargs) -> str:
    status = kwargs.pop("status", "ready")
    task_id = kb.create_task(conn, title="preflight", assignee=kwargs.pop("assignee", "default"), **kwargs)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    return task_id


def _assert_preflight_blocked_without_run(conn, task_id: str, expected: str) -> None:
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert task.current_run_id is None
    assert task.consecutive_failures == 0
    assert expected in (task.last_failure_error or "")
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert event["kind"] == "preflight_failed"
    assert expected in json.loads(event["payload"])["error"]


def test_unknown_profile_fails_before_claim(isolated_board):
    conn, _home = isolated_board
    task_id = _ready_task(conn, assignee="missing-profile")
    spawned = []

    kb.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args) or 123)

    assert spawned == []
    _assert_preflight_blocked_without_run(conn, task_id, "unknown profile 'missing-profile'")


def test_dry_run_reports_unknown_profile_preflight_without_mutation(isolated_board):
    conn, _home = isolated_board
    task_id = _ready_task(conn, assignee="missing-profile")
    spawned = []

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args: spawned.append(args) or 123,
        dry_run=True,
    )

    assert spawned == []
    assert result.spawned == []
    assert result.preflight_failed == [
        (task_id, "unknown profile 'missing-profile'"),
    ]
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "ready"
    assert task.last_failure_error is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='preflight_failed'",
        (task_id,),
    ).fetchone()[0] == 0


def test_unknown_forced_skill_fails_before_claim(isolated_board):
    conn, _home = isolated_board
    task_id = _ready_task(conn, skills=["missing-skill"])

    kb.dispatch_once(conn, spawn_fn=lambda *_args: 123)

    _assert_preflight_blocked_without_run(conn, task_id, "unknown skill 'missing-skill'")


def test_invalid_profile_toolset_fails_before_claim(isolated_board):
    conn, home = isolated_board
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - terminal\n    - missing-toolset\n",
        encoding="utf-8",
    )
    task_id = _ready_task(conn)

    kb.dispatch_once(conn, spawn_fn=lambda *_args: 123)

    _assert_preflight_blocked_without_run(conn, task_id, "unknown toolset 'missing-toolset'")


@pytest.mark.parametrize(
    ("task_kwargs", "config", "expected"),
    [
        ({"assignee": "missing-profile"}, None, "unknown profile 'missing-profile'"),
        ({"skills": ["missing-skill"]}, None, "unknown skill 'missing-skill'"),
        ({}, "platform_toolsets:\n  cli:\n    - missing-toolset\n", "unknown toolset 'missing-toolset'"),
    ],
)
def test_review_dispatch_preflight_fails_before_claim(
    isolated_board, task_kwargs, config, expected,
):
    conn, home = isolated_board
    if config:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    task_id = _ready_task(conn, status="review", **task_kwargs)
    spawned = []

    kb.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args) or 123)

    assert spawned == []
    _assert_preflight_blocked_without_run(conn, task_id, expected)


def test_preflight_uses_skill_view_resolver_for_qualified_skill(
    isolated_board, monkeypatch,
):
    conn, _home = isolated_board
    task_id = _ready_task(conn, skills=["sample:qualified"])
    calls = []

    def fake_skill_view(name, **_kwargs):
        calls.append(name)
        return json.dumps({"success": True, "name": name})

    monkeypatch.setattr("tools.skills_tool.skill_view", fake_skill_view)

    result = kb.dispatch_once(conn, spawn_fn=lambda *_args: 123)

    assert calls == ["sample:qualified"]
    assert [row[0] for row in result.spawned] == [task_id]


def test_preflight_discovers_plugin_toolsets_before_validation(
    isolated_board, monkeypatch,
):
    conn, home = isolated_board
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - plugin-dynamic\n", encoding="utf-8",
    )
    task_id = _ready_task(conn)
    discovered = []

    def fake_discover_plugins():
        from toolsets import create_custom_toolset

        discovered.append(True)
        create_custom_toolset("plugin-dynamic", "test plugin toolset")

    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", fake_discover_plugins)

    result = kb.dispatch_once(conn, spawn_fn=lambda *_args: 123)

    assert discovered
    assert [row[0] for row in result.spawned] == [task_id]


def test_valid_preflight_preserves_per_profile_capacity(isolated_board):
    conn, _home = isolated_board
    first = _ready_task(conn)
    second = _ready_task(conn)

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda task, _workspace: 1000 if task.id == first else 1001,
        max_in_progress_per_profile=1,
    )

    assert [row[0] for row in result.spawned] == [first]
    assert result.skipped_per_profile_capped == [(second, "default", 1)]
    assert kb.get_task(conn, first).status == "running"
    assert kb.get_task(conn, second).status == "ready"


@pytest.mark.parametrize(
    ("deferral", "task_kwargs", "config"),
    [
        ("cap", {"assignee": "missing-profile"}, None),
        ("cap", {"skills": ["missing-skill"]}, None),
        ("cap", {}, "platform_toolsets:\n  cli:\n    - missing-toolset\n"),
        ("guard", {"assignee": "missing-profile"}, None),
        ("guard", {"skills": ["missing-skill"]}, None),
        ("guard", {}, "platform_toolsets:\n  cli:\n    - missing-toolset\n"),
    ],
)
def test_ready_deferral_precedes_preflight_with_dry_run_live_parity(
    isolated_board,
    monkeypatch,
    deferral,
    task_kwargs,
    config,
):
    conn, home = isolated_board
    if config:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    task_id = _ready_task(conn, **task_kwargs)
    assignee = task_kwargs.get("assignee", "default")
    reconcile_orphans = False
    max_in_progress_per_profile = None

    if deferral == "cap":
        running_id = _ready_task(conn, assignee=assignee, status="running")
        max_in_progress_per_profile = 1
        expected_bucket = [(task_id, assignee, 1)]
    else:
        running_id = None
        monkeypatch.setattr(
            kb,
            "check_respawn_guard",
            lambda _conn, candidate_id, **_kw: (
                "rate_limit_cooldown" if candidate_id == task_id else None
            ),
        )
        expected_bucket = [(task_id, "rate_limit_cooldown")]

    before = conn.serialize()
    dry = kb.dispatch_once(
        conn,
        dry_run=True,
        reconcile_orphans=reconcile_orphans,
        max_in_progress_per_profile=max_in_progress_per_profile,
    )

    assert conn.serialize() == before
    assert dry.preflight_failed == []
    if deferral == "cap":
        assert dry.skipped_per_profile_capped == expected_bucket
        assert dry.respawn_guarded == []
    else:
        assert dry.respawn_guarded == expected_bucket
        assert dry.skipped_per_profile_capped == []

    live = kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args: 123,
        reconcile_orphans=reconcile_orphans,
        max_in_progress_per_profile=max_in_progress_per_profile,
    )

    assert live.preflight_failed == dry.preflight_failed
    assert live.skipped_per_profile_capped == dry.skipped_per_profile_capped
    assert live.respawn_guarded == dry.respawn_guarded
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "ready"
    assert task.current_run_id is None
    assert task.last_failure_error is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,),
    ).fetchone()[0] == 0
    events = kb.list_events(conn, task_id)
    assert not any(event.kind == "preflight_failed" for event in events)
    assert sum(event.kind == "respawn_guarded" for event in events) == (
        1 if deferral == "guard" else 0
    )
    if running_id is not None:
        running_task = kb.get_task(conn, running_id)
        assert running_task is not None
        assert running_task.status == "running"


def test_immediate_spawn_exit_is_recorded_with_log_tail(isolated_board, tmp_path, monkeypatch):
    conn, _home = isolated_board
    task_id = _ready_task(conn)
    log_path = tmp_path / "worker.log"
    leaked = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    log_path.write_text(
        f"startup failed: Unknown skill {leaked}\n", encoding="utf-8",
    )

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args: kb.SpawnedWorker(pid=4242, returncode=1, log_path=log_path),
    )

    assert result.spawned == []
    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.consecutive_failures == 1
    run = conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
    assert run["outcome"] == "crashed"
    assert "exited with code 1" in run["error"]
    assert "Unknown skill" in run["error"]
    assert leaked not in run["error"]


@pytest.mark.parametrize(
    ("returncode", "outcome", "event_kind", "failure_count", "error_fragment"),
    [
        (0, "crashed", "protocol_violation", 0, "protocol violation"),
        (kb.KANBAN_RATE_LIMIT_EXIT_CODE, "rate_limited", "rate_limited", 0, "rate-limited"),
        (1, "crashed", "crashed", 1, "exited with code 1"),
        (-signal.SIGTERM, "crashed", "crashed", 1, f"signal {signal.SIGTERM}"),
    ],
)
@pytest.mark.parametrize("status", ["ready", "review"])
def test_immediate_exit_uses_canonical_worker_taxonomy(
    isolated_board, returncode, outcome, event_kind, failure_count, error_fragment,
    status,
):
    conn, _home = isolated_board
    task_id = _ready_task(conn, status=status)

    kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args: kb.SpawnedWorker(pid=4242, returncode=returncode),
    )

    task = kb.get_task(conn, task_id)
    run = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs WHERE task_id=?", (task_id,),
    ).fetchone()
    assert task.status == "ready"
    assert task.consecutive_failures == failure_count
    assert run["outcome"] == outcome
    assert error_fragment in run["error"]
    assert any(e.kind == event_kind for e in kb.list_events(conn, task_id))


def test_healthy_spawn_result_remains_detached(isolated_board):
    conn, _home = isolated_board
    task_id = _ready_task(conn)

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args: kb.SpawnedWorker(pid=4242),
    )

    assert [row[0] for row in result.spawned] == [task_id]
    assert kb.get_task(conn, task_id).worker_pid == 4242


def test_exit_after_probe_is_classified_without_waiting_for_next_tick(isolated_board, tmp_path):
    conn, _home = isolated_board
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    kb._set_worker_pid(conn, task_id, 4242)
    log_path = tmp_path / "late.log"
    log_path.write_text("late startup failure\n", encoding="utf-8")

    assert kb.record_worker_exit(
        task_id,
        claimed.current_run_id,
        4242,
        1,
        log_path=log_path,
        db_path=kb.kanban_db_path(),
    )

    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.consecutive_failures == 1
    run = conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
    assert run["outcome"] == "crashed"
    assert "late startup failure" in run["error"]

    # A duplicate/racing notification cannot mutate the already-ended run.
    assert not kb.record_worker_exit(
        task_id,
        claimed.current_run_id,
        4242,
        1,
        log_path=log_path,
        db_path=kb.kanban_db_path(),
    )
    assert kb.get_task(conn, task_id).consecutive_failures == 1


def test_exit_watcher_retries_pid_publication_beyond_200ms(
    isolated_board, monkeypatch, tmp_path,
):
    conn, _home = isolated_board
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    attempts = []

    class Proc:
        pid = 5151

        @staticmethod
        def wait():
            return 1

    real_record = kb.record_worker_exit

    def delayed_record(*args, **kwargs):
        attempts.append(time.monotonic())
        if time.monotonic() - attempts[0] < 0.3:
            return False
        kb._set_worker_pid(conn, task_id, Proc.pid)
        return real_record(*args, **kwargs)

    monkeypatch.setattr(kb, "record_worker_exit", delayed_record)

    kb._watch_worker_exit(
        Proc(), task_id, claimed.current_run_id, tmp_path / "worker.log",
        kb.kanban_db_path(),
    )

    assert attempts[-1] - attempts[0] >= 0.3
    assert kb.get_task(conn, task_id).status == "ready"
    assert kb.get_task(conn, task_id).consecutive_failures == 1


def test_next_tick_does_not_duplicate_prompt_exit_classification(
    isolated_board, monkeypatch,
):
    conn, _home = isolated_board
    task_id = _ready_task(conn)
    kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args: kb.SpawnedWorker(pid=6161, returncode=1),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    kb.dispatch_once(conn, spawn_fn=lambda *_args: None)

    assert kb.get_task(conn, task_id).consecutive_failures == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='crashed'",
        (task_id,),
    ).fetchone()[0] == 1
