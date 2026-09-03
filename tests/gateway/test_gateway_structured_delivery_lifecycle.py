import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.platforms.base import (
    BasePlatformAdapter, DeliveryResult, Platform, PlatformConfig, SendResult,
    delivery_result_from_send,
)


_DETAILS = {
    "discord_product_details": {
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    }
}


class _LifecycleAdapter(BasePlatformAdapter):
    def __init__(self, results):
        super().__init__(PlatformConfig(), Platform.DISCORD)
        self.results = list(results)
        self.events = []
        self.metadata_seen = []
        self.handle = object()

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"chat_id": chat_id}

    async def _structured_delivery_begin(self, **kwargs):
        self.events.append(("begin", kwargs["logical_delivery_id"]))
        return self.handle

    async def _structured_delivery_attempt(self, **kwargs):
        self.events.append(("attempt", kwargs["attempt"]))
        value = dict(kwargs["metadata"])
        value["_discord_structured_delivery_handle"] = kwargs["handle"]
        return value

    async def _structured_delivery_finalize(self, **kwargs):
        self.events.append(("finalize", kwargs["outcome"]))

    async def send(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
        self.events.append(("send", content))
        self.metadata_seen.append(metadata)
        return self.results.pop(0)


class _BeginFailureAdapter(_LifecycleAdapter):
    async def _structured_delivery_begin(self, **kwargs):
        self.events.append(("begin", kwargs["logical_delivery_id"]))
        raise RuntimeError("state unavailable")


class _CancellationAdapter(_LifecycleAdapter):
    def __init__(self):
        super().__init__([])
        self.send_started = asyncio.Event()

    async def send(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
        self.events.append(("send", content))
        self.send_started.set()
        await asyncio.Future()


def test_terminal_send_results_have_explicit_success_failure_and_uncertainty():
    assert delivery_result_from_send(
        SendResult(True, delivery_certainty="delivered")
    ) == DeliveryResult(True, "certain")
    assert delivery_result_from_send(
        SendResult(False, error_kind="rejected", delivery_certainty="not_sent")
    ) == DeliveryResult(False, "not_sent", "rejected")
    # A provider-shaped object is not evidence of success.
    assert delivery_result_from_send(object()) == DeliveryResult(
        False, "uncertain", "unknown"
    )


@pytest.mark.asyncio
async def test_not_sent_retry_reuses_one_handle_and_finalizes_success_once():
    adapter = _LifecycleAdapter([
        SendResult(False, error="local reject", retryable=True, delivery_certainty="not_sent"),
        SendResult(True, message_id="m", delivery_certainty="delivered"),
    ])
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await adapter._send_with_retry("c", "summary", metadata=_DETAILS, base_delay=0)

    assert result.success
    assert [event[0] for event in adapter.events] == ["begin", "attempt", "send", "attempt", "send", "finalize"]
    assert adapter.events[-1] == ("finalize", "success")
    assert adapter.metadata_seen[0]["_discord_structured_delivery_handle"] is adapter.handle
    assert adapter.metadata_seen[1]["_discord_structured_delivery_handle"] is adapter.handle


@pytest.mark.asyncio
async def test_unknown_structured_result_is_not_retried_or_fallback_sent():
    adapter = _LifecycleAdapter([
        SendResult(False, error="connection reset", retryable=True, delivery_certainty="unknown"),
    ])
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await adapter._send_with_retry("c", "summary", metadata=_DETAILS)

    assert not result.success
    sleep.assert_not_called()
    assert [event[0] for event in adapter.events] == ["begin", "attempt", "send", "finalize"]
    assert adapter.events[-1] == ("finalize", "unknown")


@pytest.mark.asyncio
async def test_not_sent_exhaustion_finalizes_before_routing_only_fallback():
    adapter = _LifecycleAdapter([
        SendResult(False, error="invalid", retryable=False, delivery_certainty="not_sent"),
        SendResult(True, message_id="fallback"),
    ])
    result = await adapter._send_with_retry(
        "c", "summary", metadata={"thread_id": "t", **_DETAILS}
    )

    assert result.success
    assert [event[0] for event in adapter.events] == ["begin", "attempt", "send", "finalize", "send"]
    assert adapter.events[3] == ("finalize", "not_sent_exhausted")
    assert adapter.metadata_seen[-1] == {"thread_id": "t"}


@pytest.mark.asyncio
async def test_begin_failure_fails_closed_to_public_summary_with_routing_only():
    adapter = _BeginFailureAdapter([SendResult(True, message_id="summary")])

    result = await adapter._send_with_retry(
        "c", "public summary", metadata={"thread_id": "t", **_DETAILS}
    )

    assert result.success
    assert [event[0] for event in adapter.events] == ["begin", "send"]
    assert adapter.metadata_seen == [{"thread_id": "t"}]


@pytest.mark.asyncio
async def test_cancellation_finalizes_pending_once_then_reraises():
    adapter = _CancellationAdapter()
    task = asyncio.create_task(
        adapter._send_with_retry("c", "summary", metadata=_DETAILS)
    )
    await adapter.send_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event[0] for event in adapter.events] == [
        "begin", "attempt", "send", "finalize",
    ]
    assert adapter.events[-1] == ("finalize", "unknown")


@pytest.mark.asyncio
async def test_streamed_terminal_delivery_observes_one_typed_success_without_resend():
    adapter = SimpleNamespace(
        MAX_MESSAGE_LENGTH=4096,
        REQUIRES_EDIT_FINALIZE=False,
        send=AsyncMock(return_value=SendResult(True, message_id="final")),
    )
    observed = []
    from gateway.stream_consumer import GatewayStreamConsumer

    consumer = GatewayStreamConsumer(
        adapter, "chat", on_terminal_delivery=observed.append,
    )
    task = asyncio.create_task(consumer.run())
    consumer.complete("streamed response")
    await task

    assert adapter.send.await_count == 1
    assert observed == [DeliveryResult(True, "certain")]


@pytest.mark.asyncio
async def test_cancelled_stream_observes_one_typed_uncertain_outcome():
    adapter = SimpleNamespace(MAX_MESSAGE_LENGTH=4096, REQUIRES_EDIT_FINALIZE=False)
    observed = []
    from gateway.stream_consumer import GatewayStreamConsumer

    consumer = GatewayStreamConsumer(
        adapter, "chat", on_terminal_delivery=observed.append,
    )
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0)
    task.cancel()
    await task

    assert observed == [DeliveryResult(False, "uncertain", "cancelled")]


@pytest.mark.asyncio
async def test_stale_stream_early_return_does_not_read_uninitialized_terminal_state():
    adapter = SimpleNamespace(MAX_MESSAGE_LENGTH=4096, REQUIRES_EDIT_FINALIZE=False)
    observed = []
    from gateway.stream_consumer import GatewayStreamConsumer

    consumer = GatewayStreamConsumer(
        adapter, "chat", on_terminal_delivery=observed.append,
        run_still_current=lambda: False,
    )

    await consumer.run()

    assert observed == []
