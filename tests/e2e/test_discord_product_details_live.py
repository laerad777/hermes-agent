"""Opt-in live release gate for Discord private product details.

Run only with a dedicated bot/channel and explicit operator approval. The test
posts one summary, requires authorized and unauthorized clicks, reconnects a
fresh client with the persistent View, and finally verifies expiry rejection.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
import pytest

LIVE = os.environ.get("HERMES_LIVE_TESTS") == "1"
TOKEN = os.environ.get("HERMES_DISCORD_DETAILS_TEST_TOKEN", "")
CHANNEL_ID = os.environ.get("HERMES_DISCORD_DETAILS_TEST_CHANNEL_ID", "")
AUTHORIZED_USER_ID = os.environ.get("HERMES_DISCORD_DETAILS_TEST_USER_ID", "")
UNAUTHORIZED_USER_ID = os.environ.get(
    "HERMES_DISCORD_DETAILS_TEST_UNAUTHORIZED_USER_ID", ""
)
EVIDENCE_PATH = os.environ.get("HERMES_DISCORD_DETAILS_EVIDENCE_PATH", "")
TIMEOUT_SECONDS = float(os.environ.get("HERMES_DISCORD_DETAILS_TEST_TIMEOUT", "180"))


def test_live_fixture_requires_explicit_flag_and_dedicated_credentials():
    assert LIVE is (os.environ.get("HERMES_LIVE_TESTS") == "1")
    assert all(
        isinstance(value, str)
        for value in (
            TOKEN,
            CHANNEL_ID,
            AUTHORIZED_USER_ID,
            UNAUTHORIZED_USER_ID,
            EVIDENCE_PATH,
        )
    )


async def _start_client(discord, view=None, *, message_id=None):
    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    ready = asyncio.Event()
    interactions: asyncio.Queue[dict] = asyncio.Queue()

    @client.event
    async def on_ready():
        ready.set()

    @client.event
    async def on_interaction(interaction):
        custom_id = (getattr(interaction, "data", None) or {}).get("custom_id", "")
        if custom_id.startswith("hpd:v1:"):
            await interactions.put(
                {
                    "user_id": str(interaction.user.id),
                    "custom_id": custom_id,
                    "message_id": str(interaction.message.id),
                    "observed_at": time.time(),
                }
            )

    if view is not None and message_id is not None:
        client.add_view(view, message_id=int(message_id))
    task = asyncio.create_task(client.start(TOKEN))
    await asyncio.wait_for(ready.wait(), TIMEOUT_SECONDS)
    return client, task, interactions


async def _stop_client(client, task):
    await client.close()
    await asyncio.wait_for(task, 30)


async def _wait_for_user_click(queue, user_id):
    deadline = asyncio.get_running_loop().time() + TIMEOUT_SECONDS
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        event = await asyncio.wait_for(queue.get(), remaining)
        if event["user_id"] == user_id:
            return event


async def _wait_for_callback(outcomes):
    return await asyncio.wait_for(outcomes.get(), TIMEOUT_SECONDS)


def _assert_generic_unavailable(outcome):
    assert outcome["content"] == "This detail is unavailable."
    assert outcome["ephemeral"] is True
    assert outcome["allowed_mentions_none"] is True
    assert "live-secret" not in outcome["content"]


def _assert_authorized(outcome):
    assert outcome["content"] == "**Private**\nlive-secret"
    assert outcome["ephemeral"] is True
    assert outcome["allowed_mentions_none"] is True


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not LIVE, reason="live-only: set HERMES_LIVE_TESTS=1")
@pytest.mark.skipif(not TOKEN, reason="dedicated Discord details test token not configured")
@pytest.mark.skipif(not CHANNEL_ID, reason="dedicated Discord details channel not configured")
@pytest.mark.skipif(not AUTHORIZED_USER_ID, reason="authorized Discord test user not configured")
@pytest.mark.skipif(
    not UNAUTHORIZED_USER_ID,
    reason="unauthorized Discord test user not configured",
)
@pytest.mark.skipif(not EVIDENCE_PATH, reason="live evidence output path not configured")
async def test_live_persistent_view_restart_and_click_contract(tmp_path, monkeypatch):
    """Exercise authorized/unauthorized clicks, restart restore, and TTL live."""
    discord = pytest.importorskip("discord")
    from gateway.config import PlatformConfig
    from gateway.discord_product_details import validate_discord_product_details
    from plugins.platforms.discord.adapter import DiscordAdapter, ProductDetailsView
    from plugins.platforms.discord.product_details import DiscordProductDetailStore

    store = DiscordProductDetailStore(tmp_path / "state")
    envelope = validate_discord_product_details(
        {
            "items": [{"label": "details", "title": "Private", "body": "live-secret"}],
            "ttl_seconds": 30,
            "owner_user_id": AUTHORIZED_USER_ID,
        }
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token=TOKEN))
    adapter._product_details_store = store
    adapter._allowed_user_ids = {AUTHORIZED_USER_ID}
    adapter._allowed_role_ids = set()
    callback_outcomes: asyncio.Queue[dict] = asyncio.Queue()
    real_webhook_send = discord.Webhook.send

    async def recording_webhook_send(webhook, content=None, **kwargs):
        result = await real_webhook_send(webhook, content, **kwargs)
        await callback_outcomes.put(
            {
                "content": content,
                "ephemeral": kwargs.get("ephemeral") is True,
                "allowed_mentions_none": kwargs.get("allowed_mentions")
                == discord.AllowedMentions.none(),
                "observed_at": time.time(),
            }
        )
        return result

    monkeypatch.setattr(discord.Webhook, "send", recording_webhook_send)

    first, first_task, first_events = await _start_client(discord)
    adapter._client = first
    channel = first.get_channel(int(CHANNEL_ID)) or await first.fetch_channel(int(CHANNEL_ID))
    guild = getattr(channel, "guild", None)
    guild_id = str(guild.id) if guild is not None else None
    assert guild_id, "dedicated test channel must be a guild channel"
    delivery = store.prepare_delivery(
        logical_id="live-release-gate",
        envelope=envelope,
        guild_id=guild_id,
        channel_id=CHANNEL_ID,
        owner_user_id=AUTHORIZED_USER_ID,
    )
    view = ProductDetailsView(adapter, delivery, envelope)
    message = await channel.send(
        content="Hermes product-details live release gate (click the button).",
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    assert store.bind_delivery(delivery, str(message.id))

    authorized_before = await _wait_for_user_click(first_events, AUTHORIZED_USER_ID)
    authorized_before_outcome = await _wait_for_callback(callback_outcomes)
    _assert_authorized(authorized_before_outcome)
    unauthorized = await _wait_for_user_click(first_events, UNAUTHORIZED_USER_ID)
    unauthorized_outcome = await _wait_for_callback(callback_outcomes)
    _assert_generic_unavailable(unauthorized_outcome)
    await _stop_client(first, first_task)

    restored_delivery, restored_envelope, restored_message_id = (
        store.restore_active_deliveries()[0]
    )
    restored_view = ProductDetailsView(adapter, restored_delivery, restored_envelope)
    second, second_task, second_events = await _start_client(
        discord, restored_view, message_id=restored_message_id
    )
    adapter._client = second
    authorized_after_restart = await _wait_for_user_click(
        second_events, AUTHORIZED_USER_ID
    )
    authorized_after_restart_outcome = await _wait_for_callback(callback_outcomes)
    _assert_authorized(authorized_after_restart_outcome)

    await asyncio.sleep(max(0, delivery.expires_at - time.time() + 1))
    expired_click = await _wait_for_user_click(second_events, AUTHORIZED_USER_ID)
    expired_outcome = await _wait_for_callback(callback_outcomes)
    _assert_generic_unavailable(expired_outcome)
    assert store.lookup(
        expired_click["custom_id"],
        guild_id=guild_id,
        channel_id=CHANNEL_ID,
        message_id=str(message.id),
        user_id=AUTHORIZED_USER_ID,
    ) is None

    successful_history_lookups = 0
    history_messages = []
    try:
        fetched = await channel.fetch_message(message.id)
    except Exception:
        pass
    else:
        successful_history_lookups += 1
        history_messages.append(fetched)
    try:
        async for candidate in channel.history(limit=20):
            history_messages.append(candidate)
        successful_history_lookups += 1
    except Exception:
        pass
    assert successful_history_lookups > 0, "every Discord channel-history lookup failed"
    for candidate in history_messages:
        content = candidate.content or ""
        assert "live-secret" not in content
        assert "HERMES_DISCORD_DETAILS" not in content
        assert not candidate.mentions
        assert not candidate.role_mentions
        assert not candidate.mention_everyone
    await _stop_client(second, second_task)

    evidence = {
        "message_id": str(message.id),
        "authorized_before_restart": authorized_before,
        "authorized_before_restart_outcome": authorized_before_outcome,
        "unauthorized": unauthorized,
        "unauthorized_outcome": unauthorized_outcome,
        "authorized_after_restart": authorized_after_restart,
        "authorized_after_restart_outcome": authorized_after_restart_outcome,
        "expired": expired_click,
        "expired_outcome": expired_outcome,
        "guild_id": guild_id,
        "successful_history_lookups": successful_history_lookups,
    }
    output = Path(EVIDENCE_PATH).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    assert output.stat().st_size > 0
