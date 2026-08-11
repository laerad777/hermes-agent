from gateway.discord_product_details import validate_discord_product_details
from gateway.platforms.base import (
    _GatewayDeliveryResponse,
    _merge_gateway_delivery_metadata,
    _unwrap_gateway_delivery_response,
    _thread_metadata_for_source,
)
from gateway.session import Platform, SessionSource


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
