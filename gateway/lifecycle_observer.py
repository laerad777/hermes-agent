"""Bounded, product-neutral gateway lifecycle observer helpers.

The gateway owns facts about admitted turns and terminal delivery. Plugins may
observe those facts, but never receive prompt/response content through this
contract and cannot alter delivery.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass
import inspect
from typing import Any, Literal

DeliveryCertainty = Literal["certain", "uncertain", "not_sent"]
_MAX_SCALAR = 128
_MAX_CONTEXT_NOTES = 8
_MAX_CONTEXT_BYTES = 4096
_MAX_EVENT_IDS = 512
_ID_FIELDS = frozenset(
    {
        "turn_id",
        "session_id",
        "lineage_parent_session_id",
        "previous_session_id",
        "old_session_id",
        "new_session_id",
        "delivery_id",
        "message_id",
        "chat_id",
        "thread_id",
        "parent_chat_id",
        "profile",
        "route_profile",
        "platform",
        "chat_type",
    }
)
_FORBIDDEN_FIELDS = frozenset(
    {"prompt", "response", "body", "token", "tokens", "metadata", "private_metadata"}
)


@dataclass(frozen=True)
class DeliveryResult:
    """One terminal delivery outcome, independent of an adapter implementation."""

    success: bool
    certainty: DeliveryCertainty
    failure_category: str = ""


def scalar(value: Any, limit: int = _MAX_SCALAR) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("\r", " ")[:limit]


def stable_delivery_id(session_key: str, run_generation: int | None, turn_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"gateway-delivery:{scalar(session_key)}:{int(run_generation or 0)}:{scalar(turn_id)}",
        )
    )


def from_send_result(result: Any) -> DeliveryResult:
    """Conservatively normalize the existing platform SendResult shape."""
    if result is None:
        return DeliveryResult(False, "uncertain", "unknown")
    if getattr(result, "success", False) is True:
        return DeliveryResult(True, "certain")
    category = scalar(getattr(result, "error_kind", None) or "transport_failed")
    # Existing adapters do not promise whether a failed request reached the
    # platform. Only an explicit non-retryable, preflight-style declaration is
    # safe to call not_sent; absent that declaration, fail closed as uncertain.
    certainty = getattr(result, "delivery_certainty", None)
    if certainty not in {"certain", "uncertain", "not_sent"}:
        certainty = "uncertain"
    return DeliveryResult(False, certainty, category)


def _route(source: Any) -> dict[str, str]:
    platform = getattr(source, "platform", None)
    profile = getattr(source, "profile", None)
    return {
        "platform": scalar(getattr(platform, "value", platform)),
        "profile": scalar(profile),
        "route_profile": scalar(profile),
        "chat_id": scalar(getattr(source, "chat_id", None)),
        "chat_type": scalar(getattr(source, "chat_type", None)),
        "thread_id": scalar(getattr(source, "thread_id", None)),
        "parent_chat_id": scalar(getattr(source, "parent_chat_id", None)),
    }


def emit_once(
    runner: Any,
    hook_name: str,
    *,
    session_key: str,
    run_generation: int | None,
    turn_id: str,
    event_kind: str,
    **payload: Any,
) -> list[Any]:
    """Emit one bounded event per process-lifetime logical identity."""
    delivery_id = ""
    if event_kind == "delivery_result":
        delivery_id = scalar(
            payload.get("delivery_id")
            or stable_delivery_id(session_key, run_generation, turn_id)
        )
        payload["delivery_id"] = delivery_id
    key = (scalar(session_key), int(run_generation or 0), scalar(turn_id), event_kind, delivery_id)
    emitted = getattr(runner, "_gateway_observer_events", None)
    if emitted is None:
        emitted = OrderedDict()
        runner._gateway_observer_events = emitted
    if key in emitted:
        return []
    emitted[key] = None
    while len(emitted) > _MAX_EVENT_IDS:
        emitted.popitem(last=False)

    event = {
        name: scalar(value) if name in _ID_FIELDS else value
        for name, value in payload.items()
        if name not in _FORBIDDEN_FIELDS
    }
    event.update(
        {
            "event_id": str(uuid.uuid4()),
            "session_key": scalar(session_key),
            "run_generation": int(run_generation or 0),
            "turn_id": scalar(turn_id),
            "event_kind": scalar(event_kind),
        }
    )
    try:
        from hermes_cli.lifecycle import invoke_hook

        return invoke_hook(hook_name, **event)
    except Exception:
        return []


def context_notes(results: Any) -> list[str]:
    notes: list[str] = []
    for result in results if isinstance(results, list) else []:
        value = result.get("context") if isinstance(result, dict) else result
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            notes.append(value[:_MAX_CONTEXT_BYTES])
        if len(notes) >= _MAX_CONTEXT_NOTES:
            break
    return notes


def observe_origin(
    runner: Any,
    *,
    source: Any,
    session_key: str,
    session_id: str,
    lineage_parent_session_id: str | None,
    run_generation: int | None,
    turn_id: str,
    origin: str,
) -> list[str]:
    results = emit_once(
        runner,
        "on_agent_turn_origin",
        session_key=session_key,
        run_generation=run_generation,
        turn_id=turn_id,
        event_kind="agent_turn_origin",
        origin=scalar(origin),
        session_id=session_id,
        lineage_parent_session_id=lineage_parent_session_id,
        **_route(source),
    )
    return context_notes(results)


def observe_delivery(
    runner: Any,
    result: DeliveryResult,
    *,
    source: Any,
    session_key: str,
    session_id: str,
    lineage_parent_session_id: str | None,
    run_generation: int | None,
    turn_id: str,
) -> None:
    certainty: DeliveryCertainty = (
        result.certainty
        if result.certainty in {"certain", "uncertain", "not_sent"}
        else "uncertain"
    )
    emit_once(
        runner,
        "on_delivery_result",
        session_key=session_key,
        run_generation=run_generation,
        turn_id=turn_id,
        event_kind="delivery_result",
        delivery_id=stable_delivery_id(session_key, run_generation, turn_id),
        success=result.success is True,
        certainty=certainty,
        failure_category=scalar(result.failure_category),
        session_id=session_id,
        lineage_parent_session_id=lineage_parent_session_id,
        **_route(source),
    )


async def observe_runner_delivery(
    runner: Any,
    result: Any,
    *,
    source: Any,
    session_key: str,
    run_generation: int | None,
    turn_id: str,
) -> None:
    """Resolve persisted lineage and emit a conservative terminal outcome."""
    entry = None
    store = getattr(runner, "session_store", None)
    lookup = getattr(store, "lookup_by_session_key", None)
    if callable(lookup):
        try:
            entry = lookup(session_key)
            if inspect.isawaitable(entry):
                entry = await entry
        except Exception:
            entry = None
    observe_delivery(
        runner,
        result if isinstance(result, DeliveryResult) else from_send_result(result),
        source=source,
        session_key=session_key,
        session_id=str(getattr(entry, "session_id", None) or session_key),
        lineage_parent_session_id=getattr(entry, "prev_session_id", None),
        run_generation=run_generation,
        turn_id=turn_id,
    )
