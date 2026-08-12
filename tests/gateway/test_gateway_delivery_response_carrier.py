from gateway.discord_product_details import validate_discord_product_details
from gateway.discord_native import validate_discord_native_payload
from gateway.platforms.base import (
    _GatewayDeliveryResponse,
    _merge_gateway_delivery_metadata,
    _unwrap_gateway_delivery_response,
    _thread_metadata_for_source,
)
from gateway.session import Platform, SessionSource
from gateway.run import (
    _extract_discord_delivery_payload,
    _resolve_discord_delivery_payload,
)


def _envelope():
    return validate_discord_product_details({
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    })


def test_carrier_is_string_compatible_and_returns_fresh_nested_metadata():
    carrier = _GatewayDeliveryResponse(
        "summary", delivery_metadata={"discord_product_details": _envelope()}
    )
    assert isinstance(carrier, str)
    assert carrier == "summary"

    text, first = _unwrap_gateway_delivery_response(carrier)
    _, second = _unwrap_gateway_delivery_response(carrier)
    first["discord_product_details"]["items"][0]["body"] = "mutated"
    assert text == "summary"
    assert second["discord_product_details"]["items"][0]["body"] == "secret"


def test_merge_preserves_routing_and_rejects_unknown_or_conflicting_keys():
    details = {"discord_product_details": {
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    }}
    merged = _merge_gateway_delivery_metadata({"thread_id": "t"}, details)
    assert merged["thread_id"] == "t"
    assert merged["discord_product_details"]["items"][0]["body"] == "secret"

    assert _merge_gateway_delivery_metadata({"thread_id": "t"}, {"bad": 1}) == {"thread_id": "t"}
    conflict = _merge_gateway_delivery_metadata(
        {"thread_id": "t", "discord_product_details": details["discord_product_details"]},
        {"discord_product_details": {**details["discord_product_details"], "ttl_seconds": 61}},
    )
    assert conflict == {"thread_id": "t"}


def test_discord_scope_is_carried_as_delivery_routing_metadata():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel",
        user_id="user",
        chat_type="channel",
        scope_id="guild",
    )

    assert _thread_metadata_for_source(source) == {"discord_guild_id": "guild"}


def test_inbound_poll_obligation_comes_from_event_not_delivery_carrier():
    from gateway.platforms.base import MessageEvent, _poll_obligation_metadata

    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel", user_id="user",
        chat_type="channel", scope_id="guild",
    )
    event = MessageEvent(text="poll please", source=source, message_id="message-42")

    assert _poll_obligation_metadata(event) == {
        "_discord_delivery_obligation_id": "turn:message-42"
    }


def test_native_payload_carrier_is_frozen_and_unwrapped_as_fresh_mapping():
    payload = validate_discord_native_payload("user_select", {"ttl_seconds": 60})
    carrier = _GatewayDeliveryResponse(
        "summary", delivery_metadata={"discord_native_payload": payload}
    )
    _, first = _unwrap_gateway_delivery_response(carrier)
    _, second = _unwrap_gateway_delivery_response(carrier)
    first["discord_native_payload"]["payload"]["ttl_seconds"] = 900
    assert second["discord_native_payload"]["payload"]["ttl_seconds"] == 60


def test_native_and_legacy_metadata_conflict_removes_both_structured_values():
    details = {"discord_product_details": {
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    }}
    native = {"discord_native_payload": {
        "kind": "user_select", "payload": {"ttl_seconds": 60},
    }}
    merged = _merge_gateway_delivery_metadata(details, native)
    assert "discord_product_details" not in merged
    assert "discord_native_payload" not in merged


def test_gateway_extracts_native_and_requires_source_owner_for_components():
    import base64
    import json

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel",
        user_id="42",
        chat_type="channel",
        scope_id="guild",
    )
    raw = json.dumps({
        "kind": "user_select",
        "payload": {"ttl_seconds": 60},
    }, sort_keys=True, separators=(",", ":")).encode()
    marker = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    text, metadata = _extract_discord_delivery_payload(
        source, f"summary\n<!--HERMES_DISCORD_NATIVE:v1:{marker}-->"
    )
    assert text == "summary"
    assert metadata["discord_native_payload"].owner_user_id == "42"


def test_gateway_extracts_valid_legacy_product_details():
    import base64
    import json

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel",
        user_id="42",
        chat_type="channel",
        scope_id="guild",
    )
    raw = json.dumps({
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    }, sort_keys=True, separators=(",", ":")).encode()
    marker = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    text, metadata = _extract_discord_delivery_payload(
        source, f"summary\n<!--HERMES_DISCORD_DETAILS:v1:{marker}-->"
    )

    assert text == "summary"
    assert metadata is not None
    assert metadata["discord_product_details"].owner_user_id == "42"
    assert metadata["discord_product_details"].items[0].body == "secret"


def test_gateway_quarantines_mixed_or_duplicate_carriers_from_earliest_opener():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel",
        user_id="42",
        chat_type="channel",
        scope_id="guild",
    )
    native = "<!--HERMES_DISCORD_NATIVE:v1:invalid-->"
    legacy = "<!--HERMES_DISCORD_DETAILS:v1:invalid-->"

    for response in (
        f"safe\n{legacy}\n{native}",
        f"safe\n{native}\n{native}",
        f"safe\n{legacy}\n{legacy}",
    ):
        text, metadata = _extract_discord_delivery_payload(source, response)
        assert text == "safe"
        assert metadata is None


def test_gateway_quarantines_malformed_legacy_carrier_without_marker_leak():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel",
        user_id="42",
        chat_type="channel",
        scope_id="guild",
    )

    text, metadata = _extract_discord_delivery_payload(
        source, "safe\n<!--HERMES_DISCORD_DETAILS:v1:invalid-->leak"
    )

    assert text == "safe"
    assert metadata is None
    assert "HERMES_DISCORD_DETAILS" not in text
    assert "leak" not in text


def test_gateway_carries_ownerless_component_as_explicit_adapter_downgrade():
    import base64
    import json

    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel", user_id=None,
        chat_type="channel", scope_id="guild",
    )
    raw = json.dumps({
        "kind": "user_select", "payload": {"ttl_seconds": 60},
    }, sort_keys=True, separators=(",", ":")).encode()
    marker = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    text, metadata = _extract_discord_delivery_payload(
        source, f"summary\n<!--HERMES_DISCORD_NATIVE:v1:{marker}-->"
    )

    assert text == "summary"
    assert metadata is not None
    assert metadata["discord_native_payload"].owner_user_id is None


def test_gateway_overwrites_spoofed_native_owner_with_inbound_source_owner():
    import base64
    import json

    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel", user_id="real-owner",
        chat_type="channel", scope_id="guild",
    )
    raw = json.dumps({
        "kind": "user_select",
        "payload": {"ttl_seconds": 60},
        "owner_user_id": "spoofed-owner",
    }, sort_keys=True, separators=(",", ":")).encode()
    marker = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    _, metadata = _extract_discord_delivery_payload(
        source, f"summary\n<!--HERMES_DISCORD_NATIVE:v1:{marker}-->"
    )

    assert metadata is None


def test_fallback_preserves_stream_validated_metadata_for_same_clean_body():
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel", user_id="42",
        chat_type="channel", scope_id="guild",
    )
    metadata = {"discord_product_details": _envelope()}
    agent_result = {
        "final_response": "summary",
        "delivery_metadata": metadata,
        "delivery_metadata_response": "summary",
    }

    text, resolved = _resolve_discord_delivery_payload(
        source, agent_result["final_response"], agent_result
    )

    assert text == "summary"
    assert resolved == metadata


def test_fallback_rejects_stream_metadata_when_clean_body_changed():
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel", user_id="42",
        chat_type="channel", scope_id="guild",
    )
    agent_result = {
        "final_response": "changed summary",
        "delivery_metadata": {"discord_product_details": _envelope()},
        "delivery_metadata_response": "original summary",
    }

    text, metadata = _resolve_discord_delivery_payload(
        source, agent_result["final_response"], agent_result
    )

    assert text == "changed summary"
    assert metadata is None
    assert "delivery_metadata" not in agent_result
    assert "delivery_metadata_response" not in agent_result


def test_native_component_fallback_preserves_same_body_and_rejects_stale_body():
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel", user_id="42",
        chat_type="channel", scope_id="guild",
    )
    native = {
        "discord_native_payload": validate_discord_native_payload(
            "user_select", {"ttl_seconds": 60}
        )
    }
    same = {
        "final_response": "summary",
        "delivery_metadata": native,
        "delivery_metadata_response": "summary",
    }
    changed = {
        "final_response": "changed summary",
        "delivery_metadata": native,
        "delivery_metadata_response": "summary",
    }

    same_text, same_metadata = _resolve_discord_delivery_payload(
        source, same["final_response"], same
    )
    changed_text, changed_metadata = _resolve_discord_delivery_payload(
        source, changed["final_response"], changed
    )

    assert same_text == "summary"
    assert same_metadata == native
    assert changed_text == "changed summary"
    assert changed_metadata is None
    assert "delivery_metadata" not in changed
    assert "delivery_metadata_response" not in changed


def test_fallback_explicit_new_payload_replaces_preserved_stream_metadata():
    import base64
    import json

    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel", user_id="42",
        chat_type="channel", scope_id="guild",
    )
    native_raw = json.dumps({
        "kind": "user_select", "payload": {"ttl_seconds": 60},
    }, sort_keys=True, separators=(",", ":")).encode()
    native_marker = base64.urlsafe_b64encode(native_raw).decode().rstrip("=")
    response = f"summary\n<!--HERMES_DISCORD_NATIVE:v1:{native_marker}-->"
    agent_result = {
        "final_response": response,
        "delivery_metadata": {"discord_product_details": _envelope()},
        "delivery_metadata_response": "summary",
    }

    text, metadata = _resolve_discord_delivery_payload(source, response, agent_result)

    assert text == "summary"
    assert "discord_product_details" not in metadata
    assert metadata["discord_native_payload"].kind == "user_select"
    assert agent_result["delivery_metadata"] == metadata
    assert agent_result["delivery_metadata_response"] == "summary"
