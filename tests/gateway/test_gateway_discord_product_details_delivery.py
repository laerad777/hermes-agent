import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.discord_product_details import validate_discord_product_details
from gateway.stream_consumer import GatewayStreamConsumer


def _metadata():
    return {"discord_product_details": validate_discord_product_details({
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    })}


@pytest.mark.asyncio
async def test_completed_delivery_is_queue_ordered_and_only_final_gets_metadata():
    adapter = SimpleNamespace(
        MAX_MESSAGE_LENGTH=2000,
        REQUIRES_EDIT_FINALIZE=False,
        send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="m1")),
        edit_message=AsyncMock(return_value=SimpleNamespace(success=True, message_id="m1")),
    )
    consumer = GatewayStreamConsumer(adapter, "chat", metadata={"thread_id": "t"})
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("public preview")
    await asyncio.sleep(0.05)
    assert consumer.complete("public final", _metadata()) is True
    assert consumer.complete("duplicate", _metadata()) is False
    await asyncio.wait_for(task, 2)

    first = adapter.send.await_args_list[0].kwargs
    assert "discord_product_details" not in (first.get("metadata") or {})
    final = adapter.edit_message.await_args_list[-1].kwargs
    assert final["content"] == "public final"
    assert final["metadata"]["thread_id"] == "t"
    assert final["metadata"]["discord_product_details"]["items"][0]["body"] == "secret"


def test_delta_filter_quarantines_split_private_trailer():
    adapter = SimpleNamespace(MAX_MESSAGE_LENGTH=2000, REQUIRES_EDIT_FINALIZE=False)
    consumer = GatewayStreamConsumer(adapter, "chat", filter_discord_product_details=True)
    consumer.on_delta("summary\n<!--HERMES_DIS")
    consumer.on_delta("CORD_DETAILS:v1:secret-->")
    queued = []
    while not consumer._queue.empty():
        queued.append(consumer._queue.get_nowait())
    assert "".join(str(item) for item in queued) == "summary\n"
