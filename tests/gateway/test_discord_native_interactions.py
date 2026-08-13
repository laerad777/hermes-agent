import asyncio
import json
import stat
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.discord_native import validate_discord_native_payload
from gateway.platforms.base import PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.native_interactions import (
    DiscordNativeInteractionStore,
    build_native_view,
    native_route_allows,
    sanitize_ephemeral_items,
)


def _payload(kind="user_select"):
    return validate_discord_native_payload(kind, {"ttl_seconds": 60})


def test_store_persists_only_spec_and_binding_then_restores(tmp_path):
    store = DiscordNativeInteractionStore(tmp_path)
    delivery = store.prepare_delivery(
        logical_id="logical",
        envelope=_payload(),
        owner_user_id="42",
        guild_id="7",
        channel_id="8",
        now=time.time(),
    )
    assert len(delivery.custom_ids) == 1
    assert len(delivery.custom_ids[0].encode("utf-16-le")) // 2 <= 100
    assert store.resolve(
        delivery.custom_ids[0], owner_user_id="42", guild_id="7",
        channel_id="8", message_id="9",
    ) is None
    assert store.bind_delivery(delivery, "9")
    resolved = store.resolve(
        delivery.custom_ids[0], owner_user_id="42", guild_id="7",
        channel_id="8", message_id="9",
    )
    assert resolved.kind == "user_select"
    store.close()

    raw = b"".join(path.read_bytes() for path in tmp_path.iterdir() if path.is_file())
    assert b"submitted-secret" not in raw
    restarted = DiscordNativeInteractionStore(tmp_path)
    assert len(restarted.restore_active_deliveries()) == 1
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_store_rejects_forged_expired_and_wrong_binding(tmp_path):
    store = DiscordNativeInteractionStore(tmp_path)
    delivery = store.prepare_delivery(
        logical_id="logical", envelope=_payload(), owner_user_id="42",
        guild_id="7", channel_id="8", now=time.time(),
    )
    store.bind_delivery(delivery, "9")
    custom_id = delivery.custom_ids[0]
    for changed in (
        custom_id[:-1] + ("a" if custom_id[-1] != "a" else "b"),
    ):
        assert store.resolve(changed, owner_user_id="42", guild_id="7", channel_id="8", message_id="9") is None
    assert store.resolve(custom_id, owner_user_id="x", guild_id="7", channel_id="8", message_id="9") is None
    assert store.resolve(custom_id, owner_user_id="42", guild_id="x", channel_id="8", message_id="9") is None
    assert store.resolve(custom_id, owner_user_id="42", guild_id="7", channel_id="x", message_id="9") is None
    assert store.resolve(custom_id, owner_user_id="42", guild_id="7", channel_id="8", message_id="x") is None


def test_sanitizer_neutralizes_mentions_markdown_controls_and_bidi():
    text = sanitize_ephemeral_items(["@everyone **bad** ```\nline\u202e"])
    assert "@" not in text
    assert "`" not in text
    assert "\u202e" not in text
    assert "\\*\\*bad\\*\\*" in text


def test_fixed_route_matrix():
    assert native_route_allows("user_select", route="normal", chat_type="dm", owner_user_id="42")
    assert not native_route_allows("role_select", route="normal", chat_type="dm", owner_user_id="42")
    assert native_route_allows("modal", route="stream_edit", chat_type="guild_text", owner_user_id="42")
    assert not native_route_allows("poll", route="stream_edit", chat_type="guild_text", owner_user_id=None)
    assert native_route_allows("poll", route="cron_live", chat_type="guild_text", owner_user_id=None)
    assert not native_route_allows("user_select", route="cron_live", chat_type="guild_text", owner_user_id=None)
    assert not native_route_allows("user_select", route="standalone", chat_type="guild_text", owner_user_id="42")
    assert not native_route_allows("poll", route="normal", chat_type="forum", owner_user_id=None)


@pytest.mark.asyncio
async def test_streamed_modal_defers_to_structured_send_and_binds(tmp_path, monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    monkeypatch.setattr(discord_adapter, "discord", _fake_discord())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    store = DiscordNativeInteractionStore(tmp_path)
    adapter._native_interaction_store = store
    preview = SimpleNamespace(id=9, edit=AsyncMock())
    final_message = SimpleNamespace(id=10)
    channel = SimpleNamespace(
        id=8, guild=SimpleNamespace(id=7),
        send=AsyncMock(side_effect=[preview, final_message]),
        get_partial_message=lambda _message_id: preview,
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    payload = validate_discord_native_payload("modal", {
        "title": "E2E", "trigger_label": "Open", "ttl_seconds": 60,
        "inputs": [{"id": "note", "label": "Note", "style": "short"}],
    })
    payload = type(payload)(payload.kind, payload.payload, "42")
    consumer = GatewayStreamConsumer(
        adapter, "8", StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1),
    )
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("summary")
    await asyncio.sleep(0.1)
    assert consumer.complete("summary", {"discord_native_payload": payload})
    await asyncio.wait_for(task, 2)

    assert consumer.final_response_sent is False
    assert consumer.final_content_delivered is False
    preview.edit.assert_awaited_once_with(content="…")

    sent = await adapter._send_with_retry(
        "8", "summary", metadata={"discord_native_payload": payload},
    )
    assert sent.success
    assert channel.send.await_count == 2
    assert channel.send.await_args_list[-1].kwargs.get("view") is not None
    assert len(store.restore_active_deliveries()) == 1


@pytest.mark.asyncio
async def test_adapter_poll_uses_one_native_send_and_allowed_mentions_none(monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    no_mentions = object()
    monkeypatch.setattr(
        discord_adapter.discord.AllowedMentions, "none", lambda: no_mentions,
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    channel = SimpleNamespace(
        id=8,
        guild=SimpleNamespace(id=7),
        send=AsyncMock(return_value=SimpleNamespace(id=9)),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    payload = validate_discord_native_payload("poll", {
        "question": "Ship?",
        "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })

    result = await adapter.send(
        "8", "summary", metadata={
            "discord_native_payload": payload,
            "_discord_logical_delivery_id": "poll-logical",
            "_discord_poll_target_hash": "target-hash",
            "_discord_poll_obligation_hash": "obligation-hash",
            "_discord_poll_payload_hash": "payload-hash",
        },
    )

    assert result.success
    assert channel.send.await_count == 1
    kwargs = channel.send.await_args.kwargs
    assert kwargs["poll"] is not None
    assert kwargs["allowed_mentions"] is no_mentions


@pytest.mark.asyncio
async def test_streamed_poll_cleans_preview_then_ordinary_send_is_authoritative_once(
    tmp_path,
):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    preview = SimpleNamespace(id=8, edit=AsyncMock())
    poll_message = SimpleNamespace(id=9)
    channel = SimpleNamespace(
        id=7,
        guild=SimpleNamespace(id=6),
        send=AsyncMock(side_effect=[preview, poll_message]),
        get_partial_message=lambda _message_id: preview,
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    payload = validate_discord_native_payload("poll", {
        "question": "Ship?",
        "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })
    metadata = {"discord_native_payload": payload}
    consumer = GatewayStreamConsumer(
        adapter,
        "7",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1),
    )
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("summary")
    await asyncio.sleep(0.1)
    assert consumer.complete("summary", metadata)
    await asyncio.wait_for(task, 2)

    assert consumer.final_response_sent is False
    assert consumer.final_content_delivered is False
    assert consumer.already_sent is True
    preview.edit.assert_awaited_once_with(content="…")

    obligation = {
        **metadata,
        "_discord_delivery_obligation_id": "turn:inbound-message-1",
    }
    sent = await adapter._send_with_retry("7", "summary", metadata=obligation)
    replay = await adapter._send_with_retry("7", "summary", metadata=obligation)

    assert sent.success
    assert not replay.success
    assert replay.structured_failure == "poll_already_claimed"
    assert channel.send.await_count == 2
    poll_calls = [call for call in channel.send.await_args_list if "poll" in call.kwargs]
    assert len(poll_calls) == 1
    assert poll_calls[0].kwargs["content"] == "summary"
    ordinary_text_calls = [
        call for call in channel.send.await_args_list
        if "poll" not in call.kwargs
    ]
    assert len(ordinary_text_calls) == 1
    assert ordinary_text_calls[0].kwargs["content"].startswith("summary")
    assert all("HERMES_DISCORD" not in str(call) for call in channel.send.await_args_list)


@pytest.mark.asyncio
async def test_streamed_poll_cleans_every_overflow_preview_before_authoritative_send(
    tmp_path,
):
    class TinyDiscordAdapter(DiscordAdapter):
        MAX_MESSAGE_LENGTH = 24

        def max_message_length_for_chat(self, chat_id):
            return self.MAX_MESSAGE_LENGTH

        def streaming_overflow_limit(self):
            return self.MAX_MESSAGE_LENGTH

    adapter = TinyDiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    previews = {
        str(message_id): SimpleNamespace(id=message_id, edit=AsyncMock())
        for message_id in range(8, 108)
    }
    poll_message = SimpleNamespace(id=99)
    next_preview = iter(previews.values())

    async def send(**kwargs):
        if "poll" in kwargs:
            return poll_message
        return next(next_preview)

    channel = SimpleNamespace(
        id=7,
        guild=SimpleNamespace(id=6),
        send=AsyncMock(side_effect=send),
        get_partial_message=lambda message_id: previews[str(message_id)],
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    payload = validate_discord_native_payload("poll", {
        "question": "Ship?",
        "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })
    metadata = {"discord_native_payload": payload}
    consumer = GatewayStreamConsumer(
        adapter,
        "7",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
    )
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("first preview body " * 8)
    await asyncio.sleep(0.1)
    assert len(consumer._preview_message_ids) >= 2
    tracked_ids = set(consumer._preview_message_ids)
    assert consumer.complete("poll summary", metadata)
    await asyncio.wait_for(task, 2)

    neutralized_ids = {
        message_id
        for message_id, preview in previews.items()
        if preview.edit.await_count
    }
    assert neutralized_ids == tracked_ids
    assert all(
        preview.edit.await_count == 1
        for message_id, preview in previews.items()
        if message_id in tracked_ids
    )
    assert all(
        call.kwargs == {"content": "…"}
        for message_id, preview in previews.items()
        if message_id in tracked_ids
        for call in preview.edit.await_args_list
    )

    obligation = {
        **metadata,
        "_discord_delivery_obligation_id": "turn:overflow-message-1",
    }
    sent = await adapter._send_with_retry("7", "poll summary", metadata=obligation)
    sends_before_replay = channel.send.await_count
    replay = await adapter._send_with_retry("7", "poll summary", metadata=obligation)

    assert sent.success
    assert replay.structured_failure == "poll_already_claimed"
    assert channel.send.await_count == sends_before_replay
    poll_calls = [call for call in channel.send.await_args_list if "poll" in call.kwargs]
    assert len(poll_calls) == 1
    assert poll_calls[0].kwargs["content"] == "poll summary"
    assert all("HERMES_DISCORD" not in str(call) for call in channel.send.await_args_list)


def test_poll_claim_survives_crash_and_schema_has_no_vote_columns(tmp_path):
    store = DiscordNativeInteractionStore(tmp_path)
    assert store.claim_poll("stable-id", "target", "obligation", "payload") is True
    store.close()

    restarted = DiscordNativeInteractionStore(tmp_path)
    assert restarted.claim_poll("stable-id", "target", "obligation", "payload") is False
    assert restarted.poll_delivery("stable-id") == {
        "logical_id": "stable-id", "state": "claimed",
        "message_id": None, "error_class": None,
    }
    columns = {
        row[1] for row in restarted.connection.execute(
            "PRAGMA table_info(poll_deliveries)"
        )
    }
    assert columns == {
        "logical_id", "state", "message_id", "error_class",
        "target_hash", "obligation_hash", "payload_hash", "created_at", "updated_at",
    }


def test_poll_ledger_supports_distinct_target_scoped_obligations_after_restart(tmp_path):
    store = DiscordNativeInteractionStore(tmp_path)
    component = store.prepare_delivery(
        logical_id="component", envelope=_payload(), owner_user_id="42",
        guild_id="7", channel_id="8",
    )
    assert store.bind_delivery(component, "9")
    connection = store.connection
    assert connection is not None
    connection.execute("DROP TABLE poll_deliveries")
    connection.execute("""
        CREATE TABLE poll_deliveries(
            logical_id TEXT PRIMARY KEY,
            obligation_hash TEXT UNIQUE NOT NULL,
            payload_hash TEXT NOT NULL,
            state TEXT NOT NULL,
            message_id TEXT,
            error_class TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    connection.execute(
        "INSERT INTO poll_deliveries VALUES(?,?,?,?,?,?,?,?)",
        ("legacy", "obligation", "payload", "claimed", None, None, 1.0, 1.0),
    )
    connection.commit()
    store.close()

    store = DiscordNativeInteractionStore(tmp_path)
    assert len(store.restore_active_deliveries()) == 1
    assert store.poll_delivery("legacy") is None
    assert store.claim_poll(
        "target-one", "target-one-hash", "obligation", "payload",
    )
    store.close()

    restarted = DiscordNativeInteractionStore(tmp_path)
    assert restarted.claim_poll(
        "target-two", "target-two-hash", "obligation", "payload",
    )


@pytest.mark.asyncio
async def test_adapter_poll_redelivery_after_claim_makes_zero_http_sends(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    channel = SimpleNamespace(
        id=8, guild=SimpleNamespace(id=7),
        send=AsyncMock(return_value=SimpleNamespace(id=9)),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    payload = validate_discord_native_payload("poll", {
        "question": "Ship?", "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })
    metadata = {
        "discord_native_payload": payload,
        "_discord_logical_delivery_id": "same-logical",
        "_discord_poll_target_hash": "same-target",
        "_discord_poll_obligation_hash": "same-obligation",
        "_discord_poll_payload_hash": "same-payload",
    }

    first = await adapter.send("8", "summary", metadata=metadata)
    second = await adapter.send("8", "summary", metadata=metadata)

    assert first.success
    assert not second.success
    assert second.structured_failure == "poll_already_claimed"
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_adapter_poll_rejects_same_obligation_with_changed_payload(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    channel = SimpleNamespace(
        id=8, guild=SimpleNamespace(id=7),
        send=AsyncMock(return_value=SimpleNamespace(id=9)),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    first_payload = validate_discord_native_payload("poll", {
        "question": "Ship?", "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })
    changed_payload = validate_discord_native_payload("poll", {
        "question": "Ship now?", "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })

    first = await adapter.send("8", "same summary", metadata={
        "discord_native_payload": first_payload,
        "_discord_logical_delivery_id": "logical-one",
        "_discord_poll_target_hash": "same-target",
        "_discord_poll_obligation_hash": "same-obligation",
        "_discord_poll_payload_hash": "payload-one",
    })
    changed = await adapter.send("8", "same summary", metadata={
        "discord_native_payload": changed_payload,
        "_discord_logical_delivery_id": "logical-two",
        "_discord_poll_target_hash": "same-target",
        "_discord_poll_obligation_hash": "same-obligation",
        "_discord_poll_payload_hash": "payload-two",
    })

    assert first.success
    assert not changed.success
    assert changed.structured_failure == "poll_identity_mismatch"
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_same_poll_run_sends_once_to_each_target_and_replay_sends_zero(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    channels = {
        8: SimpleNamespace(
            id=8, guild=SimpleNamespace(id=7),
            send=AsyncMock(return_value=SimpleNamespace(id=81)),
        ),
        9: SimpleNamespace(
            id=9, guild=SimpleNamespace(id=7),
            send=AsyncMock(return_value=SimpleNamespace(id=91)),
        ),
    }
    adapter._client = SimpleNamespace(
        get_channel=lambda channel_id: channels.get(channel_id),
        fetch_channel=AsyncMock(side_effect=lambda channel_id: channels[channel_id]),
    )
    payload = validate_discord_native_payload("poll", {
        "question": "Ship?", "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })

    first_results = []
    for chat_id in ("8", "9"):
        first_results.append(await adapter._send_with_retry(
            chat_id, "same summary", metadata={
                "discord_native_payload": payload,
                "_discord_native_route": "cron_live",
                "_discord_delivery_obligation_id": "cron:job-1:run-1",
            },
        ))
    replay = await adapter._send_with_retry("8", "same summary", metadata={
        "discord_native_payload": payload,
        "_discord_native_route": "cron_live",
        "_discord_delivery_obligation_id": "cron:job-1:run-1",
    })

    assert all(result.success for result in first_results)
    assert not replay.success
    assert replay.structured_failure == "poll_already_claimed"
    assert channels[8].send.await_count == 1
    assert channels[9].send.await_count == 1


@pytest.mark.asyncio
async def test_adapter_disallowed_poll_fails_closed_without_plaintext_send(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    channel = SimpleNamespace(id=8, guild=SimpleNamespace(id=7), send=AsyncMock())
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    payload = validate_discord_native_payload("poll", {
        "question": "Ship?", "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })

    result = await adapter.send("8", "public summary", metadata={
        "discord_native_payload": payload,
        "_discord_native_route": "stream_edit",
        "_discord_logical_delivery_id": "poll-logical",
        "_discord_poll_obligation_hash": "poll-obligation",
        "_discord_poll_target_hash": "poll-target",
        "_discord_poll_payload_hash": "poll-payload",
    })

    assert not result.success
    assert result.structured_failure == "poll_route_disallowed"
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_forum_poll_fails_closed_without_forum_post(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    forum = SimpleNamespace(id=8, create_thread=AsyncMock())
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: forum,
        fetch_channel=AsyncMock(return_value=forum),
    )
    adapter._is_forum_parent = lambda channel: True
    payload = validate_discord_native_payload("poll", {
        "question": "Ship?", "answers": [{"text": "Yes"}, {"text": "No"}],
        "duration_hours": 24,
    })

    result = await adapter.send("8", "public summary", metadata={
        "discord_native_payload": payload,
        "_discord_native_route": "cron_live",
    })

    assert not result.success
    assert result.structured_failure == "poll_route_disallowed"
    forum.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_ownerless_component_text_fallback_is_explicit_downgrade():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    channel = SimpleNamespace(
        id=8, guild=SimpleNamespace(id=7),
        send=AsyncMock(return_value=SimpleNamespace(id=9)),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    payload = validate_discord_native_payload("user_select", {"ttl_seconds": 60})

    result = await adapter.send("8", "public summary", metadata={
        "discord_native_payload": payload,
        "_discord_native_route": "cron_live",
    })

    assert result.success
    assert result.structured_failure == "native_component_downgraded"
    assert channel.send.await_args.kwargs["content"] == "public summary"


class _FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.send_modal = AsyncMock()

    def is_done(self):
        return False


class _FakeItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeModal(_FakeItem):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.children = []

    def add_item(self, item):
        self.children.append(item)


class _FakeView:
    def __init__(self, **kwargs):
        self.children = []
        self.timeout = kwargs.get("timeout")

    def add_item(self, item):
        self.children.append(item)


def _fake_discord():
    class SelectOption(_FakeItem):
        pass

    return SimpleNamespace(
        AllowedMentions=SimpleNamespace(none=lambda: None),
        ButtonStyle=SimpleNamespace(secondary=2),
        TextStyle=SimpleNamespace(short=1, paragraph=2),
        PartialEmoji=lambda **kwargs: SimpleNamespace(**kwargs),
        SelectOption=SelectOption,
        ChannelType=lambda value: value,
        ui=SimpleNamespace(
            Modal=_FakeModal, TextInput=_FakeItem, Button=_FakeItem,
            View=_FakeView, Select=_FakeItem, UserSelect=_FakeItem,
            RoleSelect=_FakeItem, ChannelSelect=_FakeItem,
            MentionableSelect=_FakeItem,
        ),
    )


def test_string_select_converts_validated_custom_and_unicode_emoji(tmp_path):
    discord = _fake_discord()
    store = DiscordNativeInteractionStore(tmp_path)
    envelope = validate_discord_native_payload("string_select", {
        "ttl_seconds": 60,
        "options": [
            {"label": "custom", "value": "1", "emoji": {"id": "123"}},
            {"label": "unicode", "value": "2", "emoji": {"name": "✅"}},
        ],
    })
    delivery = store.prepare_delivery(
        logical_id="emoji", envelope=envelope, owner_user_id="42",
        guild_id="7", channel_id="8",
    )

    view = build_native_view(
        discord, SimpleNamespace(_native_interaction_store=store), delivery, envelope,
    )

    options = view.children[0].options
    assert options[0].emoji.id == 123
    assert options[1].emoji.name == "✅"


@pytest.mark.asyncio
async def test_open_modal_submit_revalidates_original_binding_and_expiry(tmp_path, monkeypatch):
    discord = _fake_discord()
    store = DiscordNativeInteractionStore(tmp_path)
    envelope = validate_discord_native_payload("modal", {
        "title": "Feedback", "trigger_label": "Open", "ttl_seconds": 60,
        "inputs": [{"id": "note", "label": "Note", "style": "short"}],
    })
    delivery = store.prepare_delivery(
        logical_id="modal", envelope=envelope, owner_user_id="42",
        guild_id="7", channel_id="8", now=100,
    )
    monkeypatch.setattr(time, "time", lambda: 120)
    assert store.bind_delivery(delivery, "9")
    view = build_native_view(
        discord, SimpleNamespace(_native_interaction_store=store), delivery, envelope,
    )
    opened = SimpleNamespace(
        user=SimpleNamespace(id=42), guild=SimpleNamespace(id=7),
        channel=SimpleNamespace(id=8), message=SimpleNamespace(id=9),
        response=_FakeResponse(), followup=SimpleNamespace(send_message=AsyncMock()),
    )
    await view.children[0].callback(opened)
    modal = opened.response.send_modal.await_args.args[0]
    submitted = SimpleNamespace(
        user=SimpleNamespace(id=42), guild=SimpleNamespace(id=7),
        channel=SimpleNamespace(id=8), message=None,
        response=_FakeResponse(), followup=SimpleNamespace(send_message=AsyncMock()),
    )
    monkeypatch.setattr(time, "time", lambda: 161)

    await modal.on_submit(submitted)

    submitted.response.send_message.assert_awaited_once_with(
        "This interaction is unavailable.", ephemeral=True, allowed_mentions=None,
    )


def test_native_restore_is_independent_when_product_store_is_unavailable(tmp_path, monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._product_details_store = None
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    delivery = adapter._native_interaction_store.prepare_delivery(
        logical_id="restore", envelope=_payload(), owner_user_id="42",
        guild_id="7", channel_id="8",
    )
    assert adapter._native_interaction_store.bind_delivery(delivery, "9")
    client = SimpleNamespace(add_view=MagicMock())
    restored_view = object()
    monkeypatch.setattr(
        discord_adapter, "build_native_view", lambda *args: restored_view,
    )

    adapter._restore_persistent_views(client)

    client.add_view.assert_called_once()
    assert client.add_view.call_args.args == (restored_view,)
    assert client.add_view.call_args.kwargs == {"message_id": 9}


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_outcome", [False, RuntimeError("persist exploded")])
@pytest.mark.parametrize("cleanup_raises", [False, True])
async def test_native_bind_cleanup_is_exactly_once_and_preserves_bind_exception(
    bind_outcome, cleanup_raises,
):
    message = SimpleNamespace(edit=AsyncMock())
    delivery = object()

    class Store:
        def bind_delivery(self, candidate, message_id):
            assert candidate is delivery
            assert message_id == "9"
            if isinstance(bind_outcome, Exception):
                raise bind_outcome
            return bind_outcome

        def discard_delivery(self, candidate):
            assert candidate is delivery
            if cleanup_raises:
                raise OSError("cleanup exploded")

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = Store()

    if isinstance(bind_outcome, Exception):
        with pytest.raises(RuntimeError, match="persist exploded"):
            await adapter._bind_native_delivery_or_cleanup(
                delivery, "9", message, content="summary",
            )
    else:
        assert await adapter._bind_native_delivery_or_cleanup(
            delivery, "9", message, content="summary",
        ) is False
    message.edit.assert_awaited_once_with(content="summary", view=None)


@pytest.mark.asyncio
async def test_native_forum_fallback_discards_native_pending_row(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._native_interaction_store = DiscordNativeInteractionStore(tmp_path)
    metadata = {"discord_native_payload": {
        "kind": "user_select", "payload": {"ttl_seconds": 60},
        "owner_user_id": "42",
    }}
    handle = await adapter._structured_delivery_begin(
        chat_id="123", content="summary", reply_to=None, metadata=metadata,
        logical_delivery_id="forum-native",
    )
    forum = SimpleNamespace(id=123)
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: forum,
        fetch_channel=AsyncMock(return_value=forum),
    )
    adapter._is_forum_parent = lambda channel: True
    adapter._send_to_forum = AsyncMock(return_value=SimpleNamespace(
        success=True, message_id="9", raw_response={}, delivery_certainty=None,
        structured_failure=None,
    ))

    result = await adapter.send("123", "summary", metadata={
        **metadata, "_discord_structured_delivery_handle": handle,
    })

    assert result.success
    assert result.structured_failure == "unsupported_forum"
    assert adapter._native_interaction_store.discard_pending(handle.delivery) is None


def test_native_store_rejects_database_symlink(tmp_path):
    state_dir = tmp_path / "native"
    state_dir.mkdir(mode=0o700)
    target = tmp_path / "attacker.sqlite3"
    target.write_bytes(b"")
    (state_dir / "native-v1.sqlite3").symlink_to(target)

    with pytest.raises(OSError):
        DiscordNativeInteractionStore(state_dir)


def test_native_store_rejects_signing_key_symlink(tmp_path):
    state_dir = tmp_path / "native"
    state_dir.mkdir(mode=0o700)
    target = tmp_path / "attacker-key"
    target.write_bytes(b"k" * 32)
    (state_dir / "signing-key-v1").symlink_to(target)

    with pytest.raises(OSError):
        DiscordNativeInteractionStore(state_dir)


def test_native_store_closes_directory_fd(tmp_path):
    store = DiscordNativeInteractionStore(tmp_path)
    directory_fd = store._directory_fd
    store.close()
    assert store._directory_fd is None
    if directory_fd is not None:
        with pytest.raises(OSError):
            stat.S_ISDIR(__import__("os").fstat(directory_fd).st_mode)
