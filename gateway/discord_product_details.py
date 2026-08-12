"""Bounded private-product-details protocol for Discord final responses."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

OPENER = "<!--HERMES_DISCORD_DETAILS:v1:"
CLOSER = "-->"
MAX_ENCODED_BYTES = 65_536
MAX_DECODED_BYTES = 48 * 1024


@dataclass(frozen=True, slots=True)
class DiscordProductDetailItemV1:
    label: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class DiscordProductDetailsEnvelopeV1:
    items: tuple[DiscordProductDetailItemV1, ...]
    ttl_seconds: int
    owner_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscordProductDetailsParseResult:
    public_text: str
    details: DiscordProductDetailsEnvelopeV1 | None
    reason: str | None = None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _bounded_text(value: Any, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError("invalid_text")
    return value


def _validate_mapping(value: Any) -> DiscordProductDetailsEnvelopeV1:
    if not isinstance(value, dict) or set(value) - {"items", "ttl_seconds", "owner_user_id"}:
        raise ValueError("invalid_envelope_fields")
    if set(value) < {"items", "ttl_seconds"}:
        raise ValueError("missing_envelope_fields")
    items = value["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 24:
        raise ValueError("invalid_item_count")
    frozen_items = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"label", "title", "body"}:
            raise ValueError("invalid_item_fields")
        frozen_items.append(
            DiscordProductDetailItemV1(
                label=_bounded_text(item["label"], 1, 80),
                title=_bounded_text(item["title"], 1, 256),
                body=_bounded_text(item["body"], 1, 4000),
            )
        )
    ttl = value["ttl_seconds"]
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 30 <= ttl <= 900:
        raise ValueError("invalid_ttl")
    owner = value.get("owner_user_id")
    if owner is not None and (not isinstance(owner, str) or not owner.isdecimal()):
        raise ValueError("invalid_owner")
    return DiscordProductDetailsEnvelopeV1(tuple(frozen_items), ttl, owner)


def discord_product_details_to_canonical_mapping(
    envelope: DiscordProductDetailsEnvelopeV1,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "items": [
            {"label": item.label, "title": item.title, "body": item.body}
            for item in envelope.items
        ],
        "ttl_seconds": envelope.ttl_seconds,
    }
    if envelope.owner_user_id is not None:
        value["owner_user_id"] = envelope.owner_user_id
    return value


def validate_discord_product_details(value: Any) -> DiscordProductDetailsEnvelopeV1:
    if isinstance(value, DiscordProductDetailsEnvelopeV1):
        value = discord_product_details_to_canonical_mapping(value)
    envelope = _validate_mapping(value)
    canonical = json.dumps(
        discord_product_details_to_canonical_mapping(envelope),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(canonical) > MAX_DECODED_BYTES:
        raise ValueError("decoded_too_large")
    fresh = json.loads(canonical, object_pairs_hook=_reject_duplicate_keys)
    return _validate_mapping(fresh)


def extract_discord_product_details(final_response: str) -> DiscordProductDetailsParseResult:
    text = final_response if isinstance(final_response, str) else str(final_response or "")
    index = text.find(OPENER)
    if index < 0:
        return DiscordProductDetailsParseResult(text, None)
    public = text[:index].rstrip()
    trailer = text[index:]
    if index and text[index - 1] != "\n":
        return DiscordProductDetailsParseResult(public, None, "non_trailing")
    if text.find(OPENER, index + len(OPENER)) >= 0:
        return DiscordProductDetailsParseResult(public, None, "duplicate")
    if not trailer.endswith(CLOSER):
        return DiscordProductDetailsParseResult(public, None, "incomplete")
    encoded = trailer[len(OPENER) : -len(CLOSER)]
    if not encoded or len(encoded.encode("ascii", "ignore")) > MAX_ENCODED_BYTES:
        return DiscordProductDetailsParseResult(public, None, "encoded_size")
    try:
        if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in encoded):
            raise ValueError("invalid_base64")
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        if len(raw) > MAX_DECODED_BYTES:
            raise ValueError("decoded_too_large")
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
            raise ValueError("non_canonical_base64")
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        details = validate_discord_product_details(decoded)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        return DiscordProductDetailsParseResult(public, None, str(exc))
    return DiscordProductDetailsParseResult(public, details)


class DiscordProductDetailsStreamFilter:
    """Withhold a possible opener suffix and quarantine everything after it."""

    def __init__(self, *, quarantine_limit: int = MAX_ENCODED_BYTES + 256) -> None:
        self._pending = ""
        self._quarantined = False
        self._quarantine_limit = quarantine_limit
        self._quarantine_size = 0

    def feed(self, delta: str) -> str:
        if self._quarantined:
            self._quarantine_size = min(self._quarantine_limit + 1, self._quarantine_size + len(delta))
            return ""
        combined = self._pending + (delta or "")
        index = combined.find(OPENER)
        if index >= 0:
            self._quarantined = True
            self._quarantine_size = len(combined) - index
            self._pending = ""
            return combined[:index]
        hold = min(len(combined), len(OPENER) - 1)
        for size in range(hold, -1, -1):
            if OPENER.startswith(combined[-size:] if size else ""):
                self._pending = combined[-size:] if size else ""
                return combined[:-size] if size else combined
        self._pending = ""
        return combined

    def finish(self) -> str:
        if self._quarantined:
            self._pending = ""
            return ""
        pending, self._pending = self._pending, ""
        return pending
