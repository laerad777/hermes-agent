"""Strict protocol for declarative Discord native interactions."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any

OPENER = "<!--HERMES_DISCORD_NATIVE:v1:"
LEGACY_OPENER = "<!--HERMES_DISCORD_DETAILS:v1:"
CLOSER = "-->"
MAX_ENCODED_BYTES = 65_536
MAX_DECODED_BYTES = 48 * 1024
_KINDS = frozenset({
    "modal", "string_select", "user_select", "role_select",
    "channel_select", "mentionable_select", "poll",
})
_COMMON_SELECT_KEYS = frozenset({
    "ttl_seconds", "placeholder", "min_values", "max_values", "disabled",
})
_CHANNEL_TYPES = frozenset({0, 2, 4, 5, 10, 11, 12, 13, 15, 16})
_INPUT_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")


@dataclass(frozen=True, slots=True)
class DiscordNativePayloadV1:
    kind: str
    payload: dict[str, Any]
    owner_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscordNativeParseResult:
    public_text: str
    payload: DiscordNativePayloadV1 | None
    reason: str | None = None
    carrier: str = "none"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _text(value: Any, low: int, high: int, field: str) -> str:
    if not isinstance(value, str) or not low <= _units(value) <= high:
        raise ValueError(f"invalid_{field}")
    return value


def _integer(value: Any, low: int, high: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"invalid_{field}")
    return value


def _exact(value: Any, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise ValueError("invalid_fields")
    if any(item is None for item in value.values()):
        raise ValueError("null_not_allowed")
    return value


def _common_select(value: Any, *, channel: bool = False) -> dict[str, Any]:
    allowed = set(_COMMON_SELECT_KEYS) | ({"channel_types"} if channel else set())
    source = _exact(value, {"ttl_seconds"}, allowed - {"ttl_seconds"})
    result: dict[str, Any] = {
        "ttl_seconds": _integer(source["ttl_seconds"], 30, 900, "ttl"),
        "min_values": _integer(source.get("min_values", 1), 0, 25, "min_values"),
        "max_values": _integer(source.get("max_values", 1), 1, 25, "max_values"),
    }
    disabled = source.get("disabled", False)
    if type(disabled) is not bool:
        raise ValueError("invalid_disabled")
    result["disabled"] = disabled
    if result["min_values"] > result["max_values"]:
        raise ValueError("invalid_select_range")
    if "placeholder" in source:
        result["placeholder"] = _text(source["placeholder"], 1, 150, "placeholder")
    if channel and "channel_types" in source:
        channel_types = source["channel_types"]
        if (
            not isinstance(channel_types, list)
            or not 1 <= len(channel_types) <= 10
            or any(isinstance(item, bool) or item not in _CHANNEL_TYPES for item in channel_types)
            or len(set(channel_types)) != len(channel_types)
        ):
            raise ValueError("invalid_channel_types")
        result["channel_types"] = list(channel_types)
    return result


def _modal(value: Any) -> dict[str, Any]:
    source = _exact(
        value,
        {"title", "trigger_label", "ttl_seconds", "inputs"},
        set(),
    )
    inputs = source["inputs"]
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 5:
        raise ValueError("invalid_inputs")
    frozen = []
    identifiers: set[str] = set()
    for raw in inputs:
        item = _exact(
            raw,
            {"id", "label", "style"},
            {"required", "min_length", "max_length", "placeholder"},
        )
        identifier = item["id"]
        if not isinstance(identifier, str) or not _INPUT_ID.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("invalid_input_id")
        identifiers.add(identifier)
        style = item["style"]
        if style not in {"short", "paragraph"}:
            raise ValueError("invalid_input_style")
        required = item.get("required", True)
        if type(required) is not bool:
            raise ValueError("invalid_required")
        minimum = _integer(item.get("min_length", 0), 0, 4000, "min_length")
        maximum = _integer(item.get("max_length", 4000), 1, 4000, "max_length")
        if minimum > maximum:
            raise ValueError("invalid_input_range")
        result = {
            "id": identifier,
            "label": _text(item["label"], 1, 45, "label"),
            "style": style,
            "required": required,
            "min_length": minimum,
            "max_length": maximum,
        }
        if "placeholder" in item:
            result["placeholder"] = _text(item["placeholder"], 1, 100, "placeholder")
        frozen.append(result)
    return {
        "title": _text(source["title"], 1, 45, "title"),
        "trigger_label": _text(source["trigger_label"], 1, 80, "trigger_label"),
        "ttl_seconds": _integer(source["ttl_seconds"], 30, 900, "ttl"),
        "inputs": frozen,
    }


def _string_select(value: Any) -> dict[str, Any]:
    source = _exact(
        value,
        {"ttl_seconds", "options"},
        {"placeholder", "min_values", "max_values", "disabled"},
    )
    common = _common_select({key: item for key, item in source.items() if key != "options"})
    options = source["options"]
    if not isinstance(options, list) or not 1 <= len(options) <= 25:
        raise ValueError("invalid_options")
    if common["max_values"] > len(options):
        raise ValueError("invalid_max_values")
    values: set[str] = set()
    frozen = []
    defaults = 0
    for raw in options:
        item = _exact(raw, {"label", "value"}, {"description", "default", "emoji"})
        value_text = _text(item["value"], 1, 100, "option_value")
        if value_text in values:
            raise ValueError("duplicate_option_value")
        values.add(value_text)
        result = {"label": _text(item["label"], 1, 100, "option_label"), "value": value_text}
        if "description" in item:
            result["description"] = _text(item["description"], 1, 100, "description")
        default = item.get("default", False)
        if type(default) is not bool:
            raise ValueError("invalid_default")
        result["default"] = default
        defaults += int(default)
        if "emoji" in item:
            emoji = _exact(item["emoji"], set(), {"id", "name"})
            if set(emoji) not in ({"id"}, {"name"}):
                raise ValueError("invalid_emoji")
            if "id" in emoji:
                if not isinstance(emoji["id"], str) or not emoji["id"].isdecimal() or not 1 <= len(emoji["id"]) <= 20:
                    raise ValueError("invalid_emoji_id")
            else:
                _text(emoji["name"], 1, 32, "emoji_name")
            result["emoji"] = dict(emoji)
        frozen.append(result)
    if defaults and not common["min_values"] <= defaults <= common["max_values"]:
        raise ValueError("invalid_defaults")
    return {**common, "options": frozen}


def _poll(value: Any) -> dict[str, Any]:
    source = _exact(value, {"question", "answers", "duration_hours"}, {"allow_multiselect"})
    answers = source["answers"]
    if not isinstance(answers, list) or not 2 <= len(answers) <= 10:
        raise ValueError("invalid_answers")
    multiple = source.get("allow_multiselect", False)
    if type(multiple) is not bool:
        raise ValueError("invalid_allow_multiselect")
    return {
        "question": _text(source["question"], 1, 300, "question"),
        "answers": [
            {"text": _text(_exact(answer, {"text"}, set())["text"], 1, 55, "answer")}
            for answer in answers
        ],
        "duration_hours": _integer(source["duration_hours"], 1, 768, "duration_hours"),
        "allow_multiselect": multiple,
    }


def validate_discord_native_payload(kind: Any, value: Any) -> DiscordNativePayloadV1:
    if kind not in _KINDS:
        raise ValueError("invalid_kind")
    if kind == "modal":
        payload = _modal(value)
    elif kind == "string_select":
        payload = _string_select(value)
    elif kind == "poll":
        payload = _poll(value)
    else:
        payload = _common_select(value, channel=kind == "channel_select")
    canonical = json.dumps(
        {"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode()
    if len(canonical) > MAX_DECODED_BYTES:
        raise ValueError("decoded_too_large")
    return DiscordNativePayloadV1(kind, payload)


def discord_native_to_mapping(value: DiscordNativePayloadV1) -> dict[str, Any]:
    return {"kind": value.kind, "payload": json.loads(json.dumps(value.payload))}


def extract_discord_native(final_response: str) -> DiscordNativeParseResult:
    text = final_response if isinstance(final_response, str) else str(final_response or "")
    offsets = sorted(
        index for opener in (OPENER, LEGACY_OPENER)
        for index in _all_offsets(text, opener)
    )
    if not offsets:
        return DiscordNativeParseResult(text, None)
    first = offsets[0]
    public = text[:first].rstrip()
    if len(offsets) != 1:
        return DiscordNativeParseResult(public, None, "conflict", "conflict")
    if text.startswith(LEGACY_OPENER, first):
        return DiscordNativeParseResult(public, None, "legacy", "legacy")
    if first and text[first - 1] != "\n":
        return DiscordNativeParseResult(public, None, "non_trailing", "native")
    trailer = text[first:]
    if not trailer.endswith(CLOSER):
        return DiscordNativeParseResult(public, None, "incomplete", "native")
    encoded = trailer[len(OPENER):-len(CLOSER)]
    try:
        if not encoded or len(encoded) > MAX_ENCODED_BYTES or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in encoded
        ):
            raise ValueError("invalid_base64")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if len(raw) > MAX_DECODED_BYTES or base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
            raise ValueError("invalid_size_or_canonical")
        decoded = json.loads(raw.decode(), object_pairs_hook=_pairs)
        decoded = _exact(decoded, {"kind", "payload"}, set())
        payload = validate_discord_native_payload(decoded["kind"], decoded["payload"])
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        return DiscordNativeParseResult(public, None, str(exc), "native")
    return DiscordNativeParseResult(public, payload, carrier="native")


def _all_offsets(text: str, opener: str):
    start = 0
    while (index := text.find(opener, start)) >= 0:
        yield index
        start = index + len(opener)


class DiscordNativeStreamFilter:
    """Withhold partial native/legacy openers and quarantine after either."""

    def __init__(self) -> None:
        self._pending = ""
        self._quarantined = False

    def feed(self, delta: str) -> str:
        if self._quarantined:
            return ""
        combined = self._pending + (delta or "")
        offsets = [
            index for opener in (OPENER, LEGACY_OPENER)
            if (index := combined.find(opener)) >= 0
        ]
        if offsets:
            first = min(offsets)
            self._pending = ""
            self._quarantined = True
            return combined[:first]
        hold = 0
        for size in range(1, min(len(combined), max(len(OPENER), len(LEGACY_OPENER)) - 1) + 1):
            suffix = combined[-size:]
            if OPENER.startswith(suffix) or LEGACY_OPENER.startswith(suffix):
                hold = size
        self._pending = combined[-hold:] if hold else ""
        return combined[:-hold] if hold else combined

    def finish(self) -> str:
        if self._quarantined:
            self._pending = ""
            return ""
        pending, self._pending = self._pending, ""
        return pending
