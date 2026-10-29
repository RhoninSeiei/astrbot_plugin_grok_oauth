"""Requires the real r4 observer; run explicitly with integration tests, never skip."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.provider.usage_recorder import instrument_provider, observe_usage_snapshot
from astrbot_plugin_grok_oauth.astrbot_adapter import compat
from astrbot_plugin_grok_oauth.grok_oauth.responses import ResponsesClient
from test_provider import bundle as bundle
from test_provider import provider
from test_usage_snapshot_hook import SnapshotHTTP


@pytest.mark.parametrize(
    "mode,status,total",
    [
        ("terminal", "reported", 9),
        ("intermediate", "partial", 9),
        ("missing", "missing", 0),
        ("duplicate", "reported", 9),
        ("partial", "partial", 0),
    ],
)
async def test_real_core_recovers_snapshot_after_early_close(bundle, mode, status, total):
    assert compat._observe_usage_snapshot is observe_usage_snapshot
    published = asyncio.Event()

    def observer(raw, **kwargs):
        compat.publish_usage_snapshot(raw, **kwargs)
        published.set()

    http = SnapshotHTTP(mode)
    bundle.responses = ResponsesClient(http, usage_observer=observer)
    db = SimpleNamespace(insert_provider_stat=AsyncMock())
    p = instrument_provider(provider(bundle), db)
    stream = p.text_chat_stream(prompt="a")
    assert (await anext(stream)).is_chunk
    if mode in {"intermediate", "missing"}:
        await asyncio.wait_for(http.blocked.wait(), 1)
    else:
        await asyncio.wait_for(published.wait(), 1)
    await stream.aclose()
    db.insert_provider_stat.assert_awaited_once()
    row = db.insert_provider_stat.await_args.kwargs
    assert row["status"] == "aborted"
    assert row["stats"]["usage_status"] == status
    assert (
        sum(row["stats"]["token_usage"][key] for key in ("input_other", "input_cached", "output"))
        == total
    )


async def test_real_core_normal_final_is_recorded_once(bundle):
    bundle.responses = ResponsesClient(
        SnapshotHTTP("duplicate"), usage_observer=compat.publish_usage_snapshot
    )
    db = SimpleNamespace(insert_provider_stat=AsyncMock())
    p = instrument_provider(provider(bundle), db)
    results = [item async for item in p.text_chat_stream(prompt="a")]
    assert results[-1].usage.total == 9
    db.insert_provider_stat.assert_awaited_once()
    row = db.insert_provider_stat.await_args.kwargs
    assert row["status"] == "completed" and row["stats"]["usage_status"] == "reported"
    assert (
        sum(row["stats"]["token_usage"][key] for key in ("input_other", "input_cached", "output"))
        == 9
    )


async def test_real_core_same_provider_concurrent_calls_are_isolated(bundle):
    bundle.responses = ResponsesClient(SnapshotHTTP(), usage_observer=compat.publish_usage_snapshot)
    db = SimpleNamespace(insert_provider_stat=AsyncMock())
    p = instrument_provider(provider(bundle), db)

    async def consume(label):
        return [item async for item in p.text_chat_stream(prompt=label)]

    await asyncio.gather(consume("a"), consume("b"))
    assert db.insert_provider_stat.await_count == 2
    rows = [call.kwargs for call in db.insert_provider_stat.await_args_list]
    assert sorted(
        sum(row["stats"]["token_usage"][key] for key in ("input_other", "input_cached", "output"))
        for row in rows
    ) == [9, 13]
    assert all(row["stats"]["usage_status"] == "reported" for row in rows)


@pytest.mark.parametrize(
    "mode,output,expected,status",
    [
        ("partial", 2, (8, 0, 2), "partial"),
        ("partial", 0, (8, 0, 0), "partial"),
        ("failed", 2, (8, 0, 2), "partial"),
        ("full", 1, (3, 0, 1), "reported"),
    ],
)
async def test_real_core_keeps_known_directions_without_overriding_full_final(
    bundle, mode, output, expected, status
):
    from contextlib import asynccontextmanager

    from astrbot_plugin_grok_oauth.grok_oauth.errors import StreamIncomplete

    class HTTP:
        @asynccontextmanager
        async def stream_sse(self, *args, **kwargs):
            async def chunks():
                yield SnapshotHTTP.encode(
                    {
                        "type": "response.in_progress",
                        "response": {"id": "resp-split", "usage": {"input_tokens": 8}},
                    }
                )
                usage = {"output_tokens": output}
                if mode == "full":
                    usage["input_tokens"] = 3
                yield SnapshotHTTP.encode(
                    {
                        "type": "response.failed" if mode == "failed" else "response.completed",
                        "response": {
                            "id": "resp-split",
                            "status": "failed" if mode == "failed" else "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": "ok"}],
                                }
                            ],
                            "usage": usage,
                        },
                    }
                )

            yield chunks()

    bundle.responses = ResponsesClient(HTTP(), usage_observer=compat.publish_usage_snapshot)
    db = SimpleNamespace(insert_provider_stat=AsyncMock())
    p = instrument_provider(provider(bundle), db)
    if mode == "failed":
        with pytest.raises(StreamIncomplete):
            [item async for item in p.text_chat_stream(prompt="a")]
    else:
        results = [item async for item in p.text_chat_stream(prompt="a")]
        assert results[-1].usage.input == (3 if mode == "full" else 0)
        assert results[-1].usage.output == output
    db.insert_provider_stat.assert_awaited_once()
    row = db.insert_provider_stat.await_args.kwargs
    assert row["status"] == ("error" if mode == "failed" else "completed")
    assert row["stats"]["usage_status"] == status
    assert (
        tuple(row["stats"]["token_usage"][key] for key in ("input_other", "input_cached", "output"))
        == expected
    )
