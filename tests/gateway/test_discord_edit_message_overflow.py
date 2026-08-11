"""Regression tests for Discord oversized edit_message split-and-deliver.

Issue #27881 surfaced as silent truncation: ``edit_message`` clipped any
formatted payload over Discord's 2,000-char cap to ``[:1997] + "..."`` and
returned ``success=True``, so the stream consumer believed the full reply
landed and the user lost everything past the boundary.

The fix mirrors the proven Telegram contract (and its #48648 lesson):

* **Mid-stream** (``finalize=False``) — never split.  A mid-stream split moves
  the edit target to a continuation; the next accumulated-token tick re-edits
  the full text into it and re-splits, looping forever.  We truncate a
  one-message preview in place instead.
* **Final** (``finalize=True``) — split-and-deliver: edit chunk 1 in place,
  send chunks 2..N as reply-threaded continuations, return the LAST visible id
  in ``message_id`` plus every continuation in ``continuation_message_ids``.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
from plugins.platforms.discord.product_details import DiscordProductDetailStore  # noqa: E402
from plugins.platforms.discord.product_details import secure_store_capability  # noqa: E402


requires_secure_product_store = pytest.mark.skipif(
    not secure_store_capability().available,
    reason=secure_store_capability().reason,
)


MAX = DiscordAdapter.MAX_MESSAGE_LENGTH  # 2000


def _make_adapter():
    return DiscordAdapter(PlatformConfig(enabled=True, token="***"))


def _wire_channel(adapter, *, original_msg, send_side_effect=None):
    """Wire a fake client whose channel returns ``original_msg`` on fetch and
    records every ``channel.send`` call."""
    sends = []

    async def fake_send(*, content, reference=None, view=None):
        sends.append({"content": content, "reference": reference, "view": view})
        if send_side_effect is not None:
            res = send_side_effect(len(sends), content, reference)
            if res is not None:
                return res
        return SimpleNamespace(id=9000 + len(sends))

    channel = SimpleNamespace(
        id=555,
        get_partial_message=MagicMock(return_value=original_msg),
        send=AsyncMock(side_effect=fake_send),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _cid: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    return channel, sends


# --------------------------------------------------------------------------- #
# Happy path — short edits unchanged
# --------------------------------------------------------------------------- #


class TestEditMessageHappyPath:
    @pytest.mark.asyncio
    async def test_short_edit_in_place(self):
        adapter = _make_adapter()
        edits = []
        msg = SimpleNamespace(
            id=42,
            edit=AsyncMock(side_effect=lambda *, content: edits.append(content)),
        )
        channel, sends = _wire_channel(adapter, original_msg=msg)

        result = await adapter.edit_message("555", "42", "short reply")

        assert result.success is True
        assert result.message_id == "42"
        assert result.continuation_message_ids == ()
        assert edits == ["short reply"]
        assert sends == []  # no continuations for a short edit

    @pytest.mark.asyncio
    @requires_secure_product_store
    async def test_final_edit_attaches_persistent_product_details_view(self, tmp_path):
        adapter = _make_adapter()
        adapter._product_details_store = DiscordProductDetailStore(tmp_path)
        msg = SimpleNamespace(id=42, edit=AsyncMock())
        _wire_channel(adapter, original_msg=msg)
        metadata = {
            "discord_product_details": {
                "items": [{"label": "one", "title": "A", "body": "secret"}],
                "ttl_seconds": 60,
                "owner_user_id": "123",
            }
        }

        result = await adapter.edit_message(
            "555", "42", "summary", finalize=True, metadata=metadata,
        )

        assert result.success is True
        kwargs = msg.edit.await_args.kwargs
        assert kwargs["content"] == "summary"
        assert kwargs["view"].timeout is None
        restored = adapter._product_details_store.restore_active_deliveries()
        assert len(restored) == 1
        assert restored[0][2] == "42"


    @pytest.mark.asyncio
    @pytest.mark.parametrize("cleanup_raises", [False, True])
    async def test_bind_exception_discards_once_and_preserves_original_error(
        self, cleanup_raises,
    ):
        delivery = SimpleNamespace(custom_ids=("hpd:v1:test:0:1:sig",))

        class Store:
            def __init__(self):
                self.discard_attempts = 0

            def prepare_delivery(self, **_kwargs):
                return delivery

            def bind_delivery(self, candidate, message_id):
                assert candidate is delivery
                assert message_id == "42"
                raise RuntimeError("original bind exploded")

            def discard_delivery(self, candidate):
                assert candidate is delivery
                self.discard_attempts += 1
                if cleanup_raises:
                    raise OSError("cleanup exploded")
                return True

        adapter = _make_adapter()
        store = Store()
        adapter._product_details_store = store
        msg = SimpleNamespace(id=42, edit=AsyncMock())
        _wire_channel(adapter, original_msg=msg)

        result = await adapter.edit_message(
            "555", "42", "summary", finalize=True,
            metadata={"discord_product_details": {
                "items": [{"label": "one", "title": "A", "body": "secret"}],
                "ttl_seconds": 60,
            }},
        )

        assert result.success is False
        assert result.error == "original bind exploded"
        assert store.discard_attempts == 1
        assert msg.edit.await_count == 2
        assert msg.edit.await_args.kwargs == {"content": "summary", "view": None}


# --------------------------------------------------------------------------- #
# Mid-stream overflow — TRUNCATE, never split (the #48648 lesson)
# --------------------------------------------------------------------------- #


class TestMidStreamOverflowTruncates:
    @pytest.mark.asyncio
    async def test_oversized_streaming_edit_truncates_in_place(self):
        adapter = _make_adapter()
        edits = []
        msg = SimpleNamespace(
            id=42,
            edit=AsyncMock(side_effect=lambda *, content: edits.append(content)),
        )
        channel, sends = _wire_channel(adapter, original_msg=msg)

        big = "p" * 6000
        result = await adapter.edit_message("555", "42", big, finalize=False)

        # No split: the original message stays the target, no continuations.
        assert result.success is True
        assert result.message_id == "42"
        assert result.continuation_message_ids == ()
        assert sends == [], "mid-stream overflow must NOT create continuations"
        # Exactly one in-place edit, clipped to a single chunk under the cap.
        assert len(edits) == 1
        assert len(edits[0]) <= MAX
        # No literal "..." truncation marker leaks in (fence-aware truncation).
        assert not edits[0].endswith("...")


# --------------------------------------------------------------------------- #
# Saturated-preview dedup — stop flood-control edit storms (mirrors the
# Telegram #58563 fix)
# --------------------------------------------------------------------------- #


class TestSaturatedPreviewDedup:
    @pytest.mark.asyncio
    async def test_saturated_preview_dedups_repeat_oversized_edits(self):
        """Once a mid-stream preview saturates at the truncation cap, further
        oversized edits truncate to the SAME text — re-sending them is a
        visual no-op that still counts against Discord's edit rate limit
        (the exact "Telegram #48648 lesson" this file's own docstring
        already references). The adapter must skip identical saturated
        previews without an API call."""
        adapter = _make_adapter()
        edits = []
        msg = SimpleNamespace(
            id=42,
            edit=AsyncMock(side_effect=lambda *, content: edits.append(content)),
        )
        channel, sends = _wire_channel(adapter, original_msg=msg)

        # First oversized edit: delivers the truncated preview (1 API call).
        r1 = await adapter.edit_message("555", "42", "x" * 2500, finalize=False)
        assert r1.success is True
        assert len(edits) == 1

        # Stream keeps growing within the same chunk count (2500-3500 chars
        # all truncate to the same "...(1/2)" chunk-1 preview) — no API calls.
        for grow in (3000, 3500):
            r = await adapter.edit_message("555", "42", "x" * grow, finalize=False)
            assert r.success is True
            assert r.message_id == "42"
        assert len(edits) == 1, "identical saturated previews must not be re-sent"

        # Chunk-count boundary: 4000+ chars cross into "(1/3)" — a real
        # change that SHOULD be delivered.
        await adapter.edit_message("555", "42", "x" * 4000, finalize=False)
        assert len(edits) == 2
        # ...and saturates again at the new marker.
        await adapter.edit_message("555", "42", "x" * 4500, finalize=False)
        assert len(edits) == 2

        # Finalize always delivers real content, even if identical to the
        # last saturated preview (full split-and-deliver, not a dedup skip).
        result = await adapter.edit_message("555", "42", "x" * 4500, finalize=True)
        assert result.success is True
        assert len(edits) == 3


# --------------------------------------------------------------------------- #
# Final overflow — SPLIT and deliver every chunk
# --------------------------------------------------------------------------- #


class TestFinalOverflowSplits:
    @pytest.mark.asyncio
    async def test_oversized_final_edit_splits_and_delivers(self):
        adapter = _make_adapter()
        edits = []
        msg = SimpleNamespace(
            id=42,
            to_reference=MagicMock(return_value=SimpleNamespace(kind="ref")),
            edit=AsyncMock(side_effect=lambda *, content: edits.append(content)),
        )
        channel, sends = _wire_channel(adapter, original_msg=msg)

        big = "q" * 6000  # ~3-4 chunks at 2000 cap
        result = await adapter.edit_message("555", "42", big, finalize=True)

        assert result.success is True
        # message_id points at the LAST visible continuation, not the original.
        assert result.continuation_message_ids, "expected continuations"
        assert result.message_id == result.continuation_message_ids[-1]
        # chunk 1 edited in place; chunks 2..N sent as new messages.
        assert len(edits) == 1
        assert len(sends) == len(result.continuation_message_ids)
        # Every delivered chunk is under the cap.
        for c in edits + [s["content"] for s in sends]:
            assert len(c) <= MAX
        # No "..." truncation marker anywhere.
        for c in edits + [s["content"] for s in sends]:
            assert not c.endswith("...")

    @pytest.mark.asyncio
    async def test_byte_coverage_preserved(self):
        adapter = _make_adapter()
        edits = []
        msg = SimpleNamespace(
            id=42,
            to_reference=MagicMock(return_value=object()),
            edit=AsyncMock(side_effect=lambda *, content: edits.append(content)),
        )
        channel, sends = _wire_channel(adapter, original_msg=msg)

        # Distinctive marker at the very end must survive end-to-end.
        body = "a" * 5000 + "END_MARKER_XYZ"
        result = await adapter.edit_message("555", "42", body, finalize=True)

        assert result.success is True
        delivered = "".join(edits + [s["content"] for s in sends])
        assert "END_MARKER_XYZ" in delivered

    @pytest.mark.asyncio
    @requires_secure_product_store
    async def test_product_details_view_binds_to_last_continuation(self, tmp_path):
        adapter = _make_adapter()
        adapter._product_details_store = DiscordProductDetailStore(tmp_path)
        msg = SimpleNamespace(id=42, edit=AsyncMock())
        _channel, sends = _wire_channel(adapter, original_msg=msg)
        metadata = {
            "discord_guild_id": "guild",
            "discord_product_details": {
                "items": [{"label": "one", "title": "A", "body": "secret"}],
                "ttl_seconds": 60,
            },
        }

        result = await adapter.edit_message(
            "555", "42", "x" * 5000, finalize=True, metadata=metadata,
        )

        assert result.success
        assert sends[-1]["view"] is not None
        assert all(call["view"] is None for call in sends[:-1])
        restored = adapter._product_details_store.restore_active_deliveries()
        assert restored[0][2] == result.message_id


# --------------------------------------------------------------------------- #
# Reactive overflow — Discord 50035 mid-edit triggers the same branch logic
# --------------------------------------------------------------------------- #


class TestReactiveOverflowDetection:
    @pytest.mark.asyncio
    async def test_50035_on_final_edit_triggers_split(self):
        adapter = _make_adapter()
        edit_calls = []

        # format_message leaves content under the cap, but the first edit
        # raises 50035 (server-side rejection); the split path then runs.
        def edit_effect(*, content):
            edit_calls.append(content)
            if len(edit_calls) == 1:
                raise RuntimeError(
                    "400 Bad Request (error code: 50035): Invalid Form Body\n"
                    "In content: Must be 2000 or fewer in length."
                )

        msg = SimpleNamespace(
            id=42,
            to_reference=MagicMock(return_value=object()),
            edit=AsyncMock(side_effect=edit_effect),
        )
        channel, sends = _wire_channel(adapter, original_msg=msg)

        # Content is UNDER the cap so pre-flight passes; the 50035 on edit
        # forces the reactive split.
        result = await adapter.edit_message("555", "42", "u" * 1500, finalize=True)

        assert result.success is True
        # Reactive split re-edited chunk 1 and may add continuations.
        assert len(edit_calls) >= 1

    @pytest.mark.asyncio
    @requires_secure_product_store
    async def test_single_chunk_reactive_retry_attaches_and_binds_product_view(self, tmp_path):
        adapter = _make_adapter()
        adapter._product_details_store = DiscordProductDetailStore(tmp_path)
        calls = []

        async def edit_effect(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError(
                    "400 Bad Request (error code: 50035): Invalid Form Body "
                    "Must be 2000 or fewer in length"
                )

        msg = SimpleNamespace(id=42, edit=AsyncMock(side_effect=edit_effect))
        _wire_channel(adapter, original_msg=msg)
        metadata = {"discord_product_details": {
            "items": [{"label": "one", "title": "A", "body": "secret"}],
            "ttl_seconds": 60,
        }}

        result = await adapter.edit_message(
            "555", "42", "summary", finalize=True, metadata=metadata,
        )

        assert result.success is True
        assert calls[-1]["view"] is not None
        restored = adapter._product_details_store.restore_active_deliveries()
        assert len(restored) == 1
        assert restored[0][2] == "42"


    @pytest.mark.asyncio
    @pytest.mark.parametrize("cleanup_raises", [False, True])
    async def test_single_chunk_bind_exception_discards_once_and_preserves_error(
        self, cleanup_raises,
    ):
        delivery = SimpleNamespace(custom_ids=("hpd:v1:test:0:1:sig",))

        class Store:
            def __init__(self):
                self.discard_attempts = 0

            def prepare_delivery(self, **_kwargs):
                return delivery

            def bind_delivery(self, candidate, message_id):
                assert candidate is delivery
                assert message_id == "42"
                raise RuntimeError("reactive bind exploded")

            def discard_delivery(self, candidate):
                assert candidate is delivery
                self.discard_attempts += 1
                if cleanup_raises:
                    raise OSError("cleanup exploded")
                return True

        calls = []

        async def edit_effect(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError(
                    "400 Bad Request (error code: 50035): Invalid Form Body "
                    "Must be 2000 or fewer in length"
                )

        adapter = _make_adapter()
        store = Store()
        adapter._product_details_store = store
        msg = SimpleNamespace(id=42, edit=AsyncMock(side_effect=edit_effect))
        _wire_channel(adapter, original_msg=msg)

        result = await adapter.edit_message(
            "555", "42", "summary", finalize=True,
            metadata={"discord_product_details": {
                "items": [{"label": "one", "title": "A", "body": "secret"}],
                "ttl_seconds": 60,
            }},
        )

        assert result.success is False
        assert result.error == "reactive bind exploded"
        assert store.discard_attempts == 1
        assert calls[-1] == {"content": "summary", "view": None}


# --------------------------------------------------------------------------- #
# Overflow detector helper
# --------------------------------------------------------------------------- #


class TestLengthOverflowDetector:

    def test_ignores_non_length_50035(self):
        err = RuntimeError("error code: 50035: Cannot reply to a system message")
        assert DiscordAdapter._is_length_overflow_error(err) is False



class TestPartialMessageContinuationReferences:
    """When the edit target is a PartialMessage (no to_reference — the
    no-fetch edit path), overflow continuations must still thread: the
    adapter builds the reference from ids instead of silently dropping it."""

    @pytest.mark.asyncio
    async def test_continuations_threaded_with_ids_built_reference(self):
        adapter = _make_adapter()
        partial = SimpleNamespace(id=42, edit=AsyncMock())  # no to_reference
        channel, sends = _wire_channel(adapter, original_msg=partial)

        long_text = "chunk alpha " * 600  # > MAX_MESSAGE_LENGTH
        result = await adapter.edit_message("555", "42", long_text, finalize=True)

        assert result.success is True
        assert len(sends) >= 1, "overflow should send continuations"
        for call in sends:
            assert call["reference"] is not None, (
                "continuation lost its reply reference — the ids-built "
                "fallback for PartialMessage regressed")
