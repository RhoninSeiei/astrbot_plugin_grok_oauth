"""Exercise the deployed host recorder, including error and cancellation rows."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.provider.usage_recorder import instrument_provider
from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError
from astrbot_plugin_grok_oauth.grok_oauth.responses import ResponseEvent, normalize_response
from test_provider import bundle as bundle
from test_provider import provider


@pytest.mark.parametrize(
    "raw,status,counters",
    [
        ({"input_tokens": 7}, "partial", {"input_other": 7, "input_cached": 0, "output": 0}),
        ({"output_tokens": 0}, "partial", {"input_other": 0, "input_cached": 0, "output": 0}),
        (
            {"input_tokens": 0, "output_tokens": 0},
            "reported",
            {"input_other": 0, "input_cached": 0, "output": 0},
        ),
        ({"total_tokens": 9}, "missing", {"input_other": 0, "input_cached": 0, "output": 0}),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_host_records_counter_presence(bundle, raw, status, counters, streaming):
    db = SimpleNamespace(insert_provider_stat=AsyncMock())
    bundle.responses.raw["usage"] = raw
    p = instrument_provider(provider(bundle), db)
    if streaming:
        [r async for r in p.text_chat_stream(prompt="test")]
    else:
        await p.text_chat(prompt="test")
    db.insert_provider_stat.assert_awaited_once()
    row = db.insert_provider_stat.await_args.kwargs
    assert row["status"] == "completed"
    assert row["stats"]["usage_status"] == status
    assert row["stats"]["token_usage"] == counters


@pytest.mark.parametrize("streaming", [False, True])
async def test_host_records_error_usage_once(bundle, monkeypatch, streaming):
    db = SimpleNamespace(insert_provider_stat=AsyncMock())
    bundle.responses.raw["usage"] = {"input_tokens": 7}
    p = provider(bundle)

    def reject(*args, **kwargs):
        raise ProtocolError("adapter rejected response")

    monkeypatch.setattr(p, "_response", reject)
    instrument_provider(p, db)
    with pytest.raises(ProtocolError):
        if streaming:
            [r async for r in p.text_chat_stream(prompt="test")]
        else:
            await p.text_chat(prompt="test")
    db.insert_provider_stat.assert_awaited_once()
    row = db.insert_provider_stat.await_args.kwargs
    assert row["status"] == "error"
    assert row["stats"]["usage_status"] == "partial"
    assert row["stats"]["token_usage"]["input_other"] == 7


async def test_cancel_after_terminal_usage_preserves_known_counters(bundle):
    db = SimpleNamespace(insert_provider_stat=AsyncMock())
    received = asyncio.Event()
    bundle.responses.raw["usage"] = {"input_tokens": 7, "output_tokens": 3}

    async def stream(*args, **kwargs):
        yield ResponseEvent("completed", result=normalize_response(bundle.responses.raw))
        received.set()
        await asyncio.Event().wait()

    bundle.responses.stream = stream
    p = instrument_provider(provider(bundle), db)

    async def consume():
        return [r async for r in p.text_chat_stream(prompt="test")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(received.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    db.insert_provider_stat.assert_awaited_once()
    row = db.insert_provider_stat.await_args.kwargs
    assert row["status"] == "aborted"
    assert row["stats"]["usage_status"] == "partial"
    assert row["stats"]["token_usage"] == {"input_other": 7, "input_cached": 0, "output": 3}


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_intermediate_sse_usage_survives_stop(bundle, stop):
    import json
    from contextlib import asynccontextmanager

    from astrbot_plugin_grok_oauth.grok_oauth.errors import StreamIncomplete
    from astrbot_plugin_grok_oauth.grok_oauth.responses import ResponsesClient

    received = asyncio.Event()

    class HTTP:
        @asynccontextmanager
        async def stream_sse(self, *args, **kwargs):
            async def chunks():
                event = {"type": "response.in_progress", "response": {"usage": {"input_tokens": 7}}}
                yield ("data: " + json.dumps(event) + "\n\n").encode()
                received.set()
                await asyncio.Event().wait()

            yield chunks()

    bundle.responses = ResponsesClient(HTTP())
    db = SimpleNamespace(insert_provider_stat=AsyncMock())
    p = instrument_provider(provider(bundle), db)

    async def consume():
        return [
            r
            async for r in p.text_chat_stream(
                prompt="test", timeout=0.05 if stop == "timeout" else 10
            )
        ]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(received.wait(), 1)
    if stop == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if stop == "cancel" else StreamIncomplete):
        await asyncio.wait_for(task, 1)
    db.insert_provider_stat.assert_awaited_once()
    row = db.insert_provider_stat.await_args.kwargs
    assert row["status"] == ("aborted" if stop == "cancel" else "error")
    assert row["stats"]["usage_status"] == "partial"
    assert row["stats"]["token_usage"]["input_other"] == 7
