"""Generic lifecycle observer contracts on the current upstream gateway."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.lifecycle_observer import (
    DeliveryResult,
    context_notes,
    emit_once,
    from_send_result,
    observe_runner_delivery,
)
from gateway.platforms.base import SendResult


def _runner():
    return SimpleNamespace(
        session_store=SimpleNamespace(
            lookup_by_session_key=lambda _key: SimpleNamespace(
                session_id="current", prev_session_id="prior"
            )
        )
    )


def test_origin_observer_is_deduplicated_and_bounded():
    runner = _runner()
    with patch("hermes_cli.lifecycle.invoke_hook") as invoke:
        emit_once(
            runner,
            "on_agent_turn_origin",
            session_key="s" * 300,
            run_generation=7,
            turn_id="t" * 300,
            event_kind="agent_turn_origin",
            origin="normal",
            prompt="secret prompt must not be supplied by callers",
        )
        emit_once(
            runner,
            "on_agent_turn_origin",
            session_key="s" * 300,
            run_generation=7,
            turn_id="t" * 300,
            event_kind="agent_turn_origin",
            origin="normal",
        )

    invoke.assert_called_once()
    payload = invoke.call_args.kwargs
    assert payload["session_key"] == "s" * 128
    assert payload["turn_id"] == "t" * 128
    assert payload["run_generation"] == 7
    assert "prompt" not in payload


def test_observer_dedup_cache_is_bounded():
    runner = _runner()
    with patch("hermes_cli.lifecycle.invoke_hook") as invoke:
        for index in range(600):
            emit_once(
                runner,
                "on_agent_turn_origin",
                session_key=f"session-{index}",
                run_generation=1,
                turn_id=f"turn-{index}",
                event_kind="agent_turn_origin",
            )
    assert invoke.call_count == 600
    assert len(runner._gateway_observer_events) == 512


def test_delivery_outcomes_are_conservative_and_typed():
    assert from_send_result(SendResult(True)) == DeliveryResult(True, "certain", "")
    assert from_send_result(SendResult(False, error_kind="forbidden")) == DeliveryResult(
        False, "uncertain", "forbidden"
    )
    assert from_send_result(None) == DeliveryResult(False, "uncertain", "unknown")


@pytest.mark.asyncio
async def test_delivery_observer_uses_persisted_lineage_and_stable_id():
    runner = _runner()
    source = SimpleNamespace(
        platform=SimpleNamespace(value="discord"),
        profile="profile",
        chat_id="chat",
        chat_type="dm",
        thread_id="thread",
        parent_chat_id="parent",
    )
    with patch("hermes_cli.lifecycle.invoke_hook") as invoke:
        for _ in range(2):
            await observe_runner_delivery(
                runner,
                DeliveryResult(True, "certain"),
                source=source,
                session_key="session",
                run_generation=7,
                turn_id="turn",
            )

    invoke.assert_called_once()
    payload = invoke.call_args.kwargs
    assert payload["session_id"] == "current"
    assert payload["lineage_parent_session_id"] == "prior"
    assert payload["delivery_id"]
    assert payload["certainty"] == "certain"


def test_adapter_observer_requires_a_real_delivery_attempt():
    """Silent turns are not successes; attachment sends count as attempts."""
    from gateway.platforms.base import BasePlatformAdapter

    text = Path(BasePlatformAdapter._process_message_background.__code__.co_filename).read_text()
    assert "runner is not None and delivery_attempted" in text
    assert "_record_delivery(SendResult(success=True))" in text
    assert "_record_delivery(media_result)" in text
    assert "_record_delivery(file_result)" in text


def test_hot_path_observer_hooks_are_timeout_bounded():
    from hermes_cli.plugins import _HOOK_TIMEOUT_BOUNDED_HOOKS

    assert {"on_agent_turn_origin", "on_delivery_result"} <= _HOOK_TIMEOUT_BOUNDED_HOOKS


def test_context_contributions_are_bounded():
    assert context_notes([{"context": " x "}, "y", {"context": 3}]) == ["x", "y"]
    notes = context_notes([{"context": "z" * 5000}] * 20)
    assert len(notes) == 8
    assert all(len(note) == 4096 for note in notes)


def test_ordinary_origin_without_lineage_contributes_no_continuity_note(tmp_path, monkeypatch):
    plugin_root = Path(__file__).parents[2] / "external" / "hermes-resync-plugin"
    monkeypatch.syspath_prepend(str(plugin_root))
    from hermes_resync.continuity import ContinuityStore

    store = ContinuityStore(tmp_path / "continuity.json")
    assert store.apply_origin({"origin": "normal", "run_generation": 1}) is None


def test_existing_reset_hook_records_boundary(tmp_path, monkeypatch):
    plugin_root = Path(__file__).parents[2] / "external" / "hermes-resync-plugin"
    monkeypatch.syspath_prepend(str(plugin_root))
    from hermes_resync import lifecycle
    from hermes_resync.continuity import ContinuityStore

    store = ContinuityStore(tmp_path / "continuity.json")
    monkeypatch.setattr(lifecycle, "_STORE", store)
    lifecycle.on_session_reset(
        profile="profile",
        route_profile="profile",
        platform="discord",
        chat_id="chat",
        chat_type="dm",
        thread_id="thread",
        parent_chat_id="parent",
        old_session_id="prior",
        new_session_id="current",
    )
    key = store._key(
        store.route_key(
            dict(
                profile="profile",
                platform="discord",
                chat_id="chat",
                chat_type="dm",
                thread_id="thread",
                parent_chat_id="parent",
            )
        ),
        "prior",
        "current",
    )
    assert store.records[key].state == "prior_recorded"


def test_plugin_registers_existing_reset_hook_not_parallel_boundary_api():
    plugin_root = Path(__file__).parents[2] / "external" / "hermes-resync-plugin"
    import sys

    sys.path.insert(0, str(plugin_root))
    try:
        from hermes_resync import lifecycle

        names = []

        class Context:
            def register_hook(self, name, _callback):
                names.append(name)

            def register_tool(self, **_kwargs):
                pass

        lifecycle.register(Context())
        assert names == ["on_session_reset", "on_agent_turn_origin", "on_delivery_result"]
        assert "on_session_boundary" not in names
    finally:
        sys.path.remove(str(plugin_root))


@pytest.mark.parametrize(
    ("success", "certainty", "expected"),
    [
        (True, "certain", "delivered"),
        (False, "certain", "recall_required"),
        (True, "uncertain", "recall_required"),
        (False, "not_sent", "recall_required"),
    ],
)
def test_delivery_settles_only_on_certain_success(
    tmp_path, monkeypatch, success, certainty, expected
):
    plugin_root = Path(__file__).parents[2] / "external" / "hermes-resync-plugin"
    monkeypatch.syspath_prepend(str(plugin_root))
    from hermes_resync import lifecycle
    from hermes_resync.continuity import ContinuityStore

    store = ContinuityStore(tmp_path / "continuity.json")
    monkeypatch.setattr(lifecycle, "_STORE", store)
    route = dict(
        profile="profile",
        route_profile="profile",
        platform="discord",
        chat_id="chat",
        chat_type="dm",
        thread_id="thread",
        parent_chat_id="parent",
    )
    lifecycle.on_session_reset(old_session_id="prior", new_session_id="current", **route)
    lifecycle.on_agent_turn_origin(
        event_id="origin",
        run_generation=1,
        turn_id="turn",
        session_id="current",
        lineage_parent_session_id="prior",
        **route,
    )
    lifecycle.on_delivery_result(
        event_id="delivery-event",
        delivery_id="delivery",
        run_generation=1,
        turn_id="turn",
        session_id="current",
        lineage_parent_session_id="prior",
        success=success,
        certainty=certainty,
        **route,
    )
    key = store._key(store.route_key(route), "prior", "current")
    assert store.records[key].state == expected
