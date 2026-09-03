"""Generic observer adapters; this module owns no platform adapter or delivery."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .continuity import ContinuityStore

_STORE: ContinuityStore | None = None

_EXACT_RECALL_SCHEMA = {
    "name": "hermes_resync_exact_recall",
    "description": "Check one complete, exact continuity anchor. This rejects partial, inferred, or discovery-based session lookup.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "profile": {"type": "string"},
            "platform": {"type": "string"},
            "chat_id": {"type": "string"},
            "chat_type": {"type": "string"},
            "thread_id": {"type": "string"},
            "parent_chat_id": {"type": "string"},
            "previous_session_id": {"type": "string"},
            "current_session_id": {"type": "string"},
        },
        "required": [
            "profile", "platform", "chat_id", "chat_type", "thread_id", "parent_chat_id",
            "previous_session_id", "current_session_id",
        ],
    },
}


def _event(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep only the bounded lifecycle contract, never prompt or carrier data."""
    fields = {
        "event_id", "kind", "origin", "turn_id", "profile", "route_profile", "session_id", "session_key",
        "run_generation", "platform", "chat_id", "chat_type", "thread_id", "parent_chat_id",
        "message_id", "lineage_parent_session_id", "previous_session_id", "new_session_id",
        "old_session_id", "delivery_id", "success", "certainty", "failure_category",
    }
    return {name: kwargs.get(name) for name in fields}


def _store() -> ContinuityStore:
    global _STORE
    if _STORE is None:
        root = os.environ.get("HERMES_RESYNC_DATA_DIR")
        # Refuse ambient Hermes homes: this package only writes its explicit isolated root.
        path = Path(root) / "continuity.json" if root else Path(os.devnull)
        _STORE = ContinuityStore(path)
        if not root:
            _STORE.available = False
    return _STORE


def on_session_reset(**kwargs: Any) -> None:
    """Adapt Hermes' existing reset hook into the plugin-owned boundary record."""
    event = _event(kwargs)
    event.setdefault("event_id", str(kwargs.get("event_id") or ""))
    event.setdefault("run_generation", int(kwargs.get("run_generation") or 0))
    event["previous_session_id"] = str(
        kwargs.get("old_session_id") or kwargs.get("previous_session_id") or ""
    )
    event["new_session_id"] = str(
        kwargs.get("new_session_id") or kwargs.get("session_id") or ""
    )
    _store().record_boundary(event)


def on_agent_turn_origin(**kwargs: Any) -> dict[str, str] | None:
    context = _store().apply_origin(_event(kwargs))
    return {"context": context} if context else None


def hermes_resync_exact_recall(**anchor: Any) -> dict[str, bool]:
    """Check a bounded canonical anchor without searching session history."""
    return {"exact": _store().exact_recall_anchor(anchor)}


def on_delivery_result(**kwargs: Any) -> None:
    _store().apply_delivery(_event(kwargs))


def controller_producer(
    *, run_id: str, task_id: str, candidate_digest: str, socket_path: Path, token_path: Path,
) -> Any:
    """Explicit opt-in seam for observer-only local controller reporting.

    Callers must provide a controller-preclaimed stream and both paths directly.
    This intentionally performs no environment lookup and returns no credential
    material.
    """
    from .skills import LifecycleProducer
    return LifecycleProducer.connect(
        run_id=run_id, task_id=task_id, candidate_digest=candidate_digest,
        socket_path=socket_path, token_path=token_path,
    )


def register(ctx: Any) -> None:
    """Register generic lifecycle observers and the bounded recall tool."""
    ctx.register_hook("on_session_reset", on_session_reset)
    ctx.register_hook("on_agent_turn_origin", on_agent_turn_origin)
    ctx.register_hook("on_delivery_result", on_delivery_result)
    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        register_tool(
            name="hermes_resync_exact_recall",
            toolset="hermes_resync",
            schema=_EXACT_RECALL_SCHEMA,
            handler=hermes_resync_exact_recall,
        )
