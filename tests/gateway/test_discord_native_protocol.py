import base64
import json

import pytest

from gateway.discord_native import (
    DiscordNativeStreamFilter,
    extract_discord_native,
    validate_discord_native_payload,
)


def trailer(kind, payload):
    raw = json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "<!--HERMES_DISCORD_NATIVE:v1:" + base64.urlsafe_b64encode(raw).decode().rstrip("=") + "-->"


def test_extracts_each_native_kind_from_public_text():
    payloads = {
        "modal": {
            "title": "Feedback",
            "trigger_label": "Open",
            "ttl_seconds": 60,
            "inputs": [{"id": "note", "label": "Note", "style": "paragraph"}],
        },
        "string_select": {
            "ttl_seconds": 60,
            "options": [{"label": "One", "value": "1"}],
        },
        "user_select": {"ttl_seconds": 60},
        "role_select": {"ttl_seconds": 60},
        "channel_select": {"ttl_seconds": 60, "channel_types": [0, 11]},
        "mentionable_select": {"ttl_seconds": 60},
        "poll": {
            "question": "Ship it?",
            "answers": [{"text": "Yes"}, {"text": "No"}],
            "duration_hours": 24,
        },
    }
    for kind, payload in payloads.items():
        result = extract_discord_native("summary\n" + trailer(kind, payload))
        assert result.public_text == "summary"
        assert result.payload.kind == kind


def test_duplicate_or_legacy_conflict_quarantines_from_earliest_opener():
    native = trailer("user_select", {"ttl_seconds": 60})
    legacy = "<!--HERMES_DISCORD_DETAILS:v1:abc-->"
    for text in (f"safe\n{native}\n{native}", f"safe\n{legacy}\n{native}"):
        result = extract_discord_native(text)
        assert result.public_text == "safe"
        assert result.payload is None
        assert result.reason == "conflict"


def test_invalid_or_nontrailing_marker_is_public_prefix_only():
    result = extract_discord_native("safe\n<!--HERMES_DISCORD_NATIVE:v1:not-json-->leak")
    assert result.public_text == "safe"
    assert result.payload is None
    assert result.reason


def test_modal_forbids_defaults_and_rejects_unknown_or_null_fields():
    base = {
        "title": "Feedback",
        "trigger_label": "Open",
        "ttl_seconds": 60,
        "inputs": [{"id": "note", "label": "Note", "style": "short"}],
    }
    for forbidden in ("value", "default", "initial_value"):
        bad = {**base, "inputs": [{**base["inputs"][0], forbidden: "secret"}]}
        with pytest.raises(ValueError):
            validate_discord_native_payload("modal", bad)
    with pytest.raises(ValueError):
        validate_discord_native_payload("modal", {**base, "title": None})


@pytest.mark.parametrize("kind", ["user_select", "role_select", "channel_select", "mentionable_select"])
def test_common_select_disabled_is_exact_json_boolean(kind):
    base = {"ttl_seconds": 60}
    if kind == "channel_select":
        base["channel_types"] = [0]
    assert validate_discord_native_payload(kind, {**base, "disabled": True}).payload["disabled"] is True
    assert validate_discord_native_payload(kind, {**base, "disabled": False}).payload["disabled"] is False
    for invalid in (0, 1, "false", None):
        with pytest.raises(ValueError):
            validate_discord_native_payload(kind, {**base, "disabled": invalid})


def test_common_select_defaults_are_materialized_and_bounds_are_strict():
    item = validate_discord_native_payload("user_select", {"ttl_seconds": 60})
    assert item.payload == {
        "ttl_seconds": 60,
        "min_values": 1,
        "max_values": 1,
        "disabled": False,
    }
    for bad in (
        {"ttl_seconds": True},
        {"ttl_seconds": 60, "min_values": True},
        {"ttl_seconds": 60, "min_values": 2, "max_values": 1},
        {"ttl_seconds": 60, "unknown": 1},
    ):
        with pytest.raises(ValueError):
            validate_discord_native_payload("user_select", bad)


def test_stream_filter_withholds_partial_openers_for_both_carriers():
    for opener in ("<!--HERMES_DISCORD_NATIVE:v1:", "<!--HERMES_DISCORD_DETAILS:v1:"):
        filter_ = DiscordNativeStreamFilter()
        assert filter_.feed("public\n" + opener[:12]) == "public\n"
        assert filter_.feed(opener[12:] + "hidden") == ""
        assert filter_.finish() == ""
