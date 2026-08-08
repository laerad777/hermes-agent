from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.kanban_runtime_snapshot import (
    RuntimeSnapshotError,
    bind_snapshot_lease,
    build_runtime_snapshot,
    gc_runtime_snapshots,
    mark_snapshot_lease_ready,
    prepare_snapshot_lease,
    release_snapshot_lease,
)


def _snapshot(tmp_path: Path):
    source = tmp_path / "source"
    (source / "hermes_cli").mkdir(parents=True)
    (source / "hermes_cli/__init__.py").write_text("", encoding="utf-8")
    return build_runtime_snapshot(
        source,
        repository_id="repo",
        source_revision="a" * 40,
        source_dirty=False,
        cache_root=tmp_path / "cache",
    )


def test_prepared_lease_prevents_gc_until_release(tmp_path):
    snapshot = _snapshot(tmp_path)
    lease = prepare_snapshot_lease(
        snapshot,
        cache_root=tmp_path / "cache",
        task_id="t_test",
        run_id=17,
    )

    assert json.loads(lease.path.read_text(encoding="utf-8"))["state"] == "prepared"
    assert gc_runtime_snapshots(tmp_path / "cache", max_objects=0) == []
    assert snapshot.object_root.exists()

    release_snapshot_lease(lease)
    quarantined = gc_runtime_snapshots(tmp_path / "cache", max_objects=0)
    assert quarantined
    assert not snapshot.object_root.exists()


def test_ready_then_bound_lease_retains_snapshot_until_release(tmp_path):
    snapshot = _snapshot(tmp_path)
    lease = prepare_snapshot_lease(
        snapshot,
        cache_root=tmp_path / "cache",
        task_id="t_test",
        run_id=17,
    )

    ready = mark_snapshot_lease_ready(lease, pid=4321)
    bound = bind_snapshot_lease(ready, pid=4321)

    record = json.loads(bound.path.read_text(encoding="utf-8"))
    assert record["state"] == "bound"
    assert record["pid"] == 4321
    assert bound.state == "bound"
    assert gc_runtime_snapshots(tmp_path / "cache", max_objects=0) == []

    release_snapshot_lease(bound)
    assert gc_runtime_snapshots(tmp_path / "cache", max_objects=0)


def test_lease_transition_rejects_pid_mismatch(tmp_path):
    snapshot = _snapshot(tmp_path)
    lease = prepare_snapshot_lease(
        snapshot,
        cache_root=tmp_path / "cache",
        task_id="t_test",
        run_id=17,
    )
    ready = mark_snapshot_lease_ready(lease, pid=4321)

    with pytest.raises(RuntimeSnapshotError, match="snapshot_lease_binding_mismatch"):
        bind_snapshot_lease(ready, pid=4322)


def test_lock_order_rejects_reverse_acquisition(tmp_path):
    snapshot = _snapshot(tmp_path)
    from hermes_cli import kanban_runtime_snapshot as runtime

    with runtime._ordered_lock(tmp_path / "cache", snapshot.cache_key, "lease"):
        with pytest.raises(RuntimeSnapshotError, match="cache_lock_order_violation"):
            with runtime._ordered_lock(tmp_path / "cache", snapshot.cache_key, "cache"):
                pass
