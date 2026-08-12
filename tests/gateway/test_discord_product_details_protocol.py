import base64
import json

import pytest

from gateway.discord_product_details import (
    DiscordProductDetailsEnvelopeV1,
    DiscordProductDetailsStreamFilter,
    discord_product_details_to_canonical_mapping,
    extract_discord_product_details,
)


OPENER = "<!--HERMES_DISCORD_DETAILS:v1:"


def _trailer(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{OPENER}{encoded}-->"


def _payload(**overrides):
    value = {
        "items": [{"label": "가격", "title": "상품 A", "body": "비공개 상세"}],
        "ttl_seconds": 60,
        "owner_user_id": "123",
    }
    value.update(overrides)
    return value


def test_extracts_trailing_v1_into_frozen_envelope():
    result = extract_discord_product_details("공개 요약\n" + _trailer(_payload()))

    assert result.public_text == "공개 요약"
    assert isinstance(result.details, DiscordProductDetailsEnvelopeV1)
    assert result.details.items[0].body == "비공개 상세"
    assert discord_product_details_to_canonical_mapping(result.details) == _payload()
    with pytest.raises(Exception):
        result.details.items[0].body = "mutated"


@pytest.mark.parametrize(
    "wire",
    [
        lambda: f"요약\n{OPENER}%%-->\n후속",
        lambda: f"요약\n{OPENER}e30-->",
        lambda: f"요약\n{_trailer(_payload())}\n{_trailer(_payload())}",
        lambda: f"요약 {_trailer(_payload())}",
        lambda: f"요약\n{OPENER}",
    ],
)
def test_invalid_or_incomplete_marker_is_removed_fail_closed(wire):
    result = extract_discord_product_details(wire())
    assert result.details is None
    assert OPENER not in result.public_text
    assert "비공개 상세" not in result.public_text


def test_rejects_duplicate_json_keys_unknown_fields_and_limits():
    duplicate = b'{"items":[],"items":[],"ttl_seconds":60}'
    encoded = base64.urlsafe_b64encode(duplicate).decode().rstrip("=")
    cases = [
        f"요약\n{OPENER}{encoded}-->",
        "요약\n" + _trailer({**_payload(), "unknown": True}),
        "요약\n" + _trailer(_payload(items=[])),
        "요약\n" + _trailer(_payload(items=_payload()["items"] * 25)),
        "요약\n" + _trailer(_payload(ttl_seconds=29)),
        "요약\n" + _trailer(_payload(ttl_seconds=901)),
        "요약\n" + _trailer(_payload(items=[{"label": "x", "title": "y", "body": "z" * 4001}])),
    ]
    for wire in cases:
        result = extract_discord_product_details(wire)
        assert result.details is None
        assert OPENER not in result.public_text


def test_stream_filter_never_releases_split_opener_or_payload():
    filt = DiscordProductDetailsStreamFilter()
    chunks = ["공개 요약\n<!--HERMES_DIS", "CORD_DETAILS:v1:", "secret-->"]
    visible = "".join(filt.feed(chunk) for chunk in chunks) + filt.finish()
    assert visible == "공개 요약\n"
    assert "HERMES" not in visible
    assert "secret" not in visible


def test_stream_filter_without_marker_releases_held_suffix_on_finish():
    filt = DiscordProductDetailsStreamFilter()
    visible = filt.feed("ordinary response") + filt.finish()
    assert visible == "ordinary response"
