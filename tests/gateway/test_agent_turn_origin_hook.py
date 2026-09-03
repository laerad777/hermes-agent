"""Gateway generic lifecycle observer contracts."""

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.platforms.base import DeliveryResult
from gateway.run import GatewayRunner


def _runner():
    return object.__new__(GatewayRunner)


def test_origin_observer_is_deduplicated_and_bounded():
    runner = _runner()
    with patch("hermes_cli.lifecycle.invoke_hook") as invoke:
        runner._emit_observer_hook_once(
            "on_agent_turn_origin",
            session_key="s" * 300,
            run_generation=7,
            turn_id="t" * 300,
            event_kind="agent_turn_origin",
            origin="normal",
            prompt="secret prompt must not be supplied by callers",
        )
        runner._emit_observer_hook_once(
            "on_agent_turn_origin",
            session_key="s" * 300,
            run_generation=7,
            turn_id="t" * 300,
            event_kind="agent_turn_origin",
            origin="normal",
        )

        for index in range(300):
            runner._emit_observer_hook_once(
                "on_agent_turn_origin",
                session_key=f"other-{index}",
                run_generation=7,
                turn_id=f"turn-{index}",
                event_kind="agent_turn_origin",
                origin="normal",
            )
        runner._emit_observer_hook_once(
            "on_agent_turn_origin",
            session_key="s" * 300,
            run_generation=7,
            turn_id="t" * 300,
            event_kind="agent_turn_origin",
            origin="normal",
        )

    assert invoke.call_count == 301
    payload = invoke.call_args_list[0].kwargs
    assert payload["session_key"] == "s" * 128
    assert payload["turn_id"] == "t" * 128
    assert payload["run_generation"] == 7
    assert uuid.UUID(payload["event_id"])
    assert "prompt" not in payload


def test_delivery_outcomes_are_bounded_to_safe_categories():
    from gateway.run import _delivery_observer_outcome

    assert _delivery_observer_outcome(DeliveryResult(True, "certain")) == (True, "certain", "")
    assert _delivery_observer_outcome(DeliveryResult(False, "not_sent", "rejected")) == (False, "not_sent", "rejected")
    assert _delivery_observer_outcome(None) == (False, "uncertain", "unknown")


def test_typed_delivery_observer_preserves_terminal_routing_identity():
    runner = _runner()
    source = SimpleNamespace(
        platform=SimpleNamespace(value="test"), profile="profile", chat_id="chat",
        chat_type="dm", thread_id="thread", parent_chat_id="parent",
        lineage_parent_session_id="untrusted-source-lineage",
    )
    runner._cache_session_entry("session", SimpleNamespace(prev_session_id="lineage"))
    with patch("hermes_cli.lifecycle.invoke_hook") as invoke:
        runner._observe_delivery_result(
            DeliveryResult(False, "uncertain", "timeout"),
            source=source, session_key="session", run_generation=7,
            turn_id="turn", delivery_id="delivery",
        )

    payload = invoke.call_args.kwargs
    assert payload["event_id"] != payload["delivery_id"]
    assert payload["delivery_id"] == "delivery"
    assert payload["session_key"] == "session"
    assert payload["lineage_parent_session_id"] == "lineage"
    assert payload["run_generation"] == 7
    assert payload["turn_id"] == "turn"
    assert payload["certainty"] == "uncertain"


def test_duplicate_terminal_delivery_uses_one_stable_process_lifetime_id():
    runner = _runner()
    source = SimpleNamespace(
        platform=SimpleNamespace(value="test"), profile="profile", chat_id="chat",
        chat_type="dm", thread_id="thread", parent_chat_id="parent",
    )
    runner._cache_session_entry("session", SimpleNamespace(session_id="current", prev_session_id="prior"))
    with patch("hermes_cli.lifecycle.invoke_hook") as invoke:
        for _ in range(2):
            runner._observe_delivery_result(
                DeliveryResult(True, "certain"), source=source, session_key="session",
                run_generation=7, turn_id="turn", delivery_id="",
            )

    invoke.assert_called_once()
    assert invoke.call_args.kwargs["delivery_id"]


def test_ordinary_origin_without_lineage_contributes_no_continuity_note(tmp_path, monkeypatch):
    plugin_root = Path(__file__).parents[2] / "external" / "hermes-resync-plugin"
    monkeypatch.syspath_prepend(str(plugin_root))
    from hermes_resync.continuity import ContinuityStore

    store = ContinuityStore(tmp_path / "continuity.json")
    assert store.apply_origin({"origin": "normal", "run_generation": 1}) is None


@pytest.mark.parametrize(
    ("success", "certainty", "expected_state"),
    [(True, "certain", "delivered"), (False, "certain", "recall_required"),
     (True, "uncertain", "recall_required"), (False, "not_sent", "recall_required")],
)
def test_core_invoke_hook_settles_resync_delivery_only_for_certain_string(
    tmp_path, monkeypatch, success, certainty, expected_state,
):
    """The runner's typed event reaches the plugin through the core dispatcher."""
    plugin_root = Path(__file__).parents[2] / "external" / "hermes-resync-plugin"
    monkeypatch.syspath_prepend(str(plugin_root))
    from hermes_cli import lifecycle as core_lifecycle
    from hermes_cli import plugins
    from hermes_resync import lifecycle as resync_lifecycle
    from hermes_resync.continuity import ContinuityStore

    manager = plugins.PluginManager()

    class Context:
        def register_hook(self, name, callback):
            manager._hooks.setdefault(name, []).append(callback)

    resync_lifecycle.register(Context())
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    monkeypatch.setattr(resync_lifecycle, "_STORE", ContinuityStore(tmp_path / "continuity.json"))

    source = SimpleNamespace(
        platform=SimpleNamespace(value="discord"), profile="profile", chat_id="chat",
        chat_type="dm", thread_id="thread", parent_chat_id="parent",
    )
    route = dict(profile="profile", platform="discord", chat_id="chat", chat_type="dm",
                 thread_id="thread", parent_chat_id="parent")
    core_lifecycle.invoke_hook(
        "on_session_boundary", event_id="boundary", run_generation=1,
        lineage_parent_session_id="prior", previous_session_id="prior",
        new_session_id="current", session_id="current", **route,
    )
    runner = _runner()
    runner._cache_session_entry(
        "session", SimpleNamespace(prev_session_id="prior", session_id="current")
    )
    runner._emit_observer_hook_once(
        "on_agent_turn_origin", session_key="session", run_generation=1,
        turn_id="turn", event_kind="agent_turn_origin", session_id="current",
        lineage_parent_session_id="prior", **route,
    )
    runner._observe_delivery_result(
        DeliveryResult(success, certainty), source=source, session_key="session",
        run_generation=1, turn_id="turn", delivery_id="delivery",
    )

    record = resync_lifecycle._STORE.records[resync_lifecycle._STORE._key(
        resync_lifecycle._STORE.route_key(route), "prior", "current"
    )]
    assert record.state == expected_state


def test_origin_plugin_context_return_becomes_turn_local_sidecar_contribution():
    from gateway.run import _observer_context_notes

    runner = _runner()
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"context": "recall"}]) as invoke:
        results = runner._emit_observer_hook_once(
            "on_agent_turn_origin",
            session_key="session",
            run_generation=1,
            turn_id="message",
            event_kind="agent_turn_origin",
            profile="profile",
            platform="test",
        )

    assert invoke.call_args.kwargs["profile"] == "profile"
    assert _observer_context_notes(results) == ["recall"]
