"""Plugin bridge contract tests; these do not substitute for r4 ledger tests."""

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest
from astrbot_plugin_grok_oauth.astrbot_adapter import compat
from astrbot_plugin_grok_oauth.grok_oauth.responses import ResponsesClient
from test_provider import bundle as bundle
from test_provider import provider


class SnapshotHTTP:
    def __init__(self, mode="terminal"):
        self.mode = mode
        self.blocked = asyncio.Event()

    @asynccontextmanager
    async def stream_sse(self, path, *, json, policy):
        label = json["input"][-1]["content"][0]["text"]
        count = 7 if label == "a" else 11
        raw = {
            "id": "resp-" + label,
            "status": "completed",
            "model": "grok-4.6",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": count, "output_tokens": 2},
        }

        if self.mode == "partial":
            raw["usage"] = {"input_tokens": 0}

        async def chunks():
            yield self.encode({"type": "response.output_text.delta", "delta": "o"})
            await asyncio.sleep(0)
            if self.mode in {"intermediate", "missing"}:
                event = {"type": "response.in_progress", "response": {"id": raw["id"]}}
                if self.mode == "intermediate":
                    event["response"]["usage"] = raw["usage"]
                yield self.encode(event)
                self.blocked.set()
                await asyncio.Event().wait()
            if self.mode == "duplicate":
                for _ in range(2):
                    yield self.encode({"type": "response.in_progress", "response": raw})
            yield self.encode({"type": "response.completed", "response": raw})

        yield chunks()

    @staticmethod
    def encode(event):
        return ("data: " + json.dumps(event) + "\n\n").encode()


@pytest.mark.parametrize("mode", ["terminal", "intermediate", "missing", "duplicate", "partial"])
async def test_bridge_publishes_before_final_is_consumed(bundle, monkeypatch, mode):
    seen = []
    published = asyncio.Event()

    def observer(usage, **kwargs):
        seen.append((usage.total, usage.is_partial, kwargs))
        published.set()

    monkeypatch.setattr(compat, "_observe_usage_snapshot", observer)
    http = SnapshotHTTP(mode)
    bundle.responses = ResponsesClient(http, usage_observer=compat.publish_usage_snapshot)
    stream = provider(bundle).text_chat_stream(prompt="a")
    first = await anext(stream)
    assert first.is_chunk
    if mode in {"intermediate", "missing"}:
        await asyncio.wait_for(http.blocked.wait(), 1)
    else:
        await asyncio.wait_for(published.wait(), 1)
    await stream.aclose()
    if mode == "missing":
        assert seen == []
    else:
        assert all(
            total == (0 if mode == "partial" else 9) and partial == (mode == "partial")
            for total, partial, _ in seen
        )
        assert seen[-1][2] == {
            "response_id": "resp-a",
            "complete": mode != "intermediate",
            "reported_fields": ("input",) if mode == "partial" else ("input", "output"),
        }
        assert len(seen) == (3 if mode == "duplicate" else 1)


async def test_bridge_retains_request_context_in_same_provider_producers(bundle, monkeypatch):
    scope = ContextVar("test_snapshot_scope", default=None)

    def observer(usage, **kwargs):
        target = scope.get()
        assert target is not None
        target.append((usage.total, kwargs["response_id"]))

    monkeypatch.setattr(compat, "_observe_usage_snapshot", observer)
    bundle.responses = ResponsesClient(SnapshotHTTP(), usage_observer=compat.publish_usage_snapshot)
    p = provider(bundle)

    async def request(label):
        seen = []
        token = scope.set(seen)
        try:
            result = [item async for item in p.text_chat_stream(prompt=label)]
            return seen, result[-1].usage.total
        finally:
            scope.reset(token)

    a, b = await asyncio.gather(request("a"), request("b"))
    assert a == ([(9, "resp-a")], 9)
    assert b == ([(13, "resp-b")], 13)


async def test_optional_hook_absence_keeps_existing_response(bundle, monkeypatch):
    monkeypatch.setattr(compat, "_observe_usage_snapshot", None)
    bundle.responses = ResponsesClient(SnapshotHTTP(), usage_observer=compat.publish_usage_snapshot)
    result = [item async for item in provider(bundle).text_chat_stream(prompt="a")]
    assert result[-1].usage.total == 9


def test_bridge_skips_counters_with_no_direction(monkeypatch):
    seen = []
    monkeypatch.setattr(compat, "_observe_usage_snapshot", lambda *a, **kw: seen.append((a, kw)))
    compat.publish_usage_snapshot({"input_tokens_details": {"cached_tokens": 8}}, complete=True)
    assert seen == []


async def test_runtime_wires_parsed_usage_to_optional_host_hook(tmp_path, monkeypatch):
    import httpx
    from astrbot_plugin_grok_oauth.astrbot_adapter.runtime import GrokRuntime
    from astrbot_plugin_grok_oauth.grok_oauth.models import RequestPolicy, TokenSnapshot

    seen = []
    monkeypatch.setattr(
        compat, "_observe_usage_snapshot", lambda usage, **kw: seen.append((usage.total, kw))
    )

    class OAuth:
        async def get_token(self, **kwargs):
            return TokenSnapshot("default", "test-access", "refresh", None, "s", "c", 1, 0)

    raw = {
        "id": "resp-test",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    runtime = GrokRuntime(
        {}, tmp_path, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw))
    )
    runtime.http._oauth = OAuth()
    try:
        result = await runtime.responses.create({"model": "grok"}, policy=RequestPolicy())
        assert result.text == "ok"
        assert seen == [
            (
                5,
                {
                    "response_id": "resp-test",
                    "complete": True,
                    "reported_fields": ("input", "output"),
                },
            )
        ]
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "raw,fields",
    [
        ({"input_tokens": 0}, ("input",)),
        ({"output_tokens": 0}, ("output",)),
    ],
)
def test_bridge_reports_explicit_zero_direction(raw, fields, monkeypatch):
    seen = []
    monkeypatch.setattr(
        compat, "_observe_usage_snapshot", lambda usage, **kw: seen.append((usage, kw))
    )
    compat.publish_usage_snapshot(raw, response_id="resp-zero", complete=True)
    assert len(seen) == 1
    usage, metadata = seen[0]
    assert usage.total == 0 and usage.is_partial
    assert metadata == {"response_id": "resp-zero", "complete": True, "reported_fields": fields}
