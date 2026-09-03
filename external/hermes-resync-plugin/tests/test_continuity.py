from __future__ import annotations

import json
import stat
from pathlib import Path

from hermes_resync.continuity import ContinuityStore


def event(**overrides):
    base = {
        "event_id": "event-1",
        "profile": "profile",
        "platform": "platform",
        "chat_id": "chat",
        "chat_type": "channel",
        "thread_id": "thread",
        "parent_chat_id": "parent",
        "run_generation": 1,
        "previous_session_id": "old-session",
        "new_session_id": "new-session",
    }
    base.update(overrides)
    return base


def boundary(store: ContinuityStore, **overrides):
    store.record_boundary(event(**overrides))


def test_automatic_lineage_cross_generation_requires_exact_recall(tmp_path: Path):
    store = ContinuityStore(tmp_path / "state.json")
    boundary(store)

    result = store.apply_origin(event(event_id="origin-1", run_generation=2, lineage_parent_session_id="old-session"))

    assert "exact prior session 'old-session'" in result
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600


def test_explicit_lineage_uses_the_explicit_parent(tmp_path: Path):
    store = ContinuityStore(tmp_path / "state.json")
    boundary(store, previous_session_id="boundary-parent")
    boundary(store, previous_session_id="explicit-parent", event_id="event-2")

    result = store.apply_origin(event(event_id="origin-2", lineage_parent_session_id="explicit-parent"))

    assert "explicit-parent" in result


def test_stale_and_duplicate_events_cannot_replace_newer_state(tmp_path: Path):
    store = ContinuityStore(tmp_path / "state.json")
    boundary(store, run_generation=3)
    origin = event(event_id="origin", run_generation=4, lineage_parent_session_id="old-session")
    assert store.apply_origin(origin)
    assert store.apply_origin(origin) is None
    assert store.apply_origin(event(event_id="stale", run_generation=2, lineage_parent_session_id="old-session")) is None


def test_restart_loads_record_and_corrupt_or_missing_lineage_fails_closed(tmp_path: Path):
    state = tmp_path / "state.json"
    first = ContinuityStore(state)
    boundary(first)
    restarted = ContinuityStore(state)
    assert "old-session" in restarted.apply_origin(event(event_id="after-restart", lineage_parent_session_id="old-session"))

    state.write_text("not json", encoding="utf-8")
    corrupt = ContinuityStore(state)
    assert "Continuity unavailable" in corrupt.apply_origin(event(event_id="corrupt", lineage_parent_session_id="old-session"))
    missing = ContinuityStore(tmp_path / "missing.json")
    assert "Continuity unavailable" in missing.apply_origin(event(event_id="missing", lineage_parent_session_id="old-session"))


def test_failed_or_uncertain_delivery_retains_gate_and_only_certain_success_delivers(tmp_path: Path):
    store = ContinuityStore(tmp_path / "state.json")
    boundary(store)
    origin = event(event_id="origin", lineage_parent_session_id="old-session")
    assert store.apply_origin(origin)
    store.apply_delivery(event(event_id="delivery-event-1", delivery_id="delivery-1", lineage_parent_session_id="old-session", success=False, certainty="certain"))
    assert store.apply_origin(event(event_id="origin-2", lineage_parent_session_id="old-session"))
    store.apply_delivery(event(event_id="delivery-event-2", delivery_id="delivery-2", lineage_parent_session_id="old-session", success=True, certainty="uncertain"))
    assert store.apply_origin(event(event_id="origin-3", lineage_parent_session_id="old-session"))
    store.apply_delivery(event(event_id="delivery-event-3", delivery_id="delivery-3", lineage_parent_session_id="old-session", success=True, certainty="certain"))
    assert store.apply_origin(event(event_id="origin-4", lineage_parent_session_id="old-session")) is None


def test_atomic_state_is_valid_json_after_each_write(tmp_path: Path):
    state = tmp_path / "state.json"
    store = ContinuityStore(state)
    boundary(store)
    assert json.loads(state.read_text(encoding="utf-8"))["version"] == 2


def test_exact_recall_requires_lossless_profile_route_and_session_anchor(tmp_path: Path):
    store = ContinuityStore(tmp_path / "state.json")
    boundary(store)
    origin = event(event_id="origin", lineage_parent_session_id="old-session")
    assert store.apply_origin(origin)
    assert store.exact_recall(origin) is True
    assert store.exact_recall(event(lineage_parent_session_id="old-session", profile="other-profile")) is False
    assert store.exact_recall(event(lineage_parent_session_id="old-session", route_profile="other-profile")) is False
    assert store.exact_recall(event(lineage_parent_session_id="old-session", new_session_id="other-session")) is False
    assert store.exact_recall(event(lineage_parent_session_id="old-session", new_session_id="")) is False


def test_exact_recall_anchor_rejects_partial_alias_and_discovery_inputs(tmp_path: Path):
    store = ContinuityStore(tmp_path / "state.json")
    boundary(store)
    assert store.apply_origin(event(event_id="origin", lineage_parent_session_id="old-session"))
    anchor = {
        "profile": "profile", "platform": "platform", "chat_id": "chat", "chat_type": "channel",
        "thread_id": "thread", "parent_chat_id": "parent", "previous_session_id": "old-session",
        "current_session_id": "new-session",
    }

    assert store.exact_recall_anchor(anchor) is True
    assert store.exact_recall_anchor({**anchor, "profile": "other-profile"}) is False
    assert store.exact_recall_anchor({key: value for key, value in anchor.items() if key != "current_session_id"}) is False
    assert store.exact_recall_anchor({**anchor, "session_search": "old-session"}) is False
    assert store.exact_recall_anchor({**anchor, "previous_session_id": 1}) is False


def test_expired_records_compact_to_tombstones_then_expire(tmp_path: Path):
    now = [100.0]
    store = ContinuityStore(tmp_path / "state.json", ttl_seconds=10, tombstone_ttl_seconds=5, clock=lambda: now[0])
    boundary(store)
    assert len(store.records) == 1
    now[0] = 111.0
    assert "Continuity unavailable" in store.apply_origin(event(event_id="expired", lineage_parent_session_id="old-session"))
    assert store.records == {}
    assert len(store.tombstones) == 1
    now[0] = 117.0
    store.apply_origin(event(event_id="after-tombstone", lineage_parent_session_id="old-session"))
    assert store.tombstones == {}
