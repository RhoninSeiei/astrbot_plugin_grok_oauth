import asyncio
import json
import time

import httpx
import pytest

from grok_oauth.errors import OutcomeUnknown, ProtocolError, RateLimited
from grok_oauth.http import AuthorizedHttp
from grok_oauth.models import RequestPolicy, TokenSnapshot


class OAuth:
    async def get_token(self, **kwargs):
        return TokenSnapshot("test", "synthetic-access", "synthetic-refresh", None, "s", "c")


class Log:
    def __init__(self):
        self.records = []

    def info(self, template, value):
        self.records.append(json.loads(value))


async def invoke(wire, streaming, policy):
    if streaming:
        async with wire.stream_sse("/responses", json={}, policy=policy) as chunks:
            return b"".join([chunk async for chunk in chunks])
    return await wire.request_json("POST", "/responses", json={}, policy=policy)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("remaining", [0.2, 90])
async def test_connection_budget_is_separate_and_bounded(streaming, remaining):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await invoke(
            AuthorizedHttp(client, OAuth()),
            streaming,
            RequestPolicy(deadline=time.monotonic() + remaining),
        )
    assert 0 < seen[0]["connect"] <= min(10, remaining)
    assert 0 < seen[0]["read"] <= remaining
    if remaining > 10:
        assert seen[0]["read"] > 80


@pytest.mark.parametrize("streaming", [False, True])
async def test_safe_connection_retry_has_backoff_and_succeeds(streaming):
    starts = []
    log = Log()

    def handler(request):
        starts.append(time.monotonic())
        if len(starts) == 1:
            raise httpx.ConnectError("SECRET_ERROR", request=request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await invoke(
            AuthorizedHttp(client, OAuth(), diagnostic_logging=True, diagnostic_logger=log),
            streaming,
            RequestPolicy(deadline=time.monotonic() + 2),
        )
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.08
    assert log.records[0]["connect_retries"] == 1
    assert log.records[0]["retry_backoff_ms"] >= 80
    assert "SECRET_ERROR" not in json.dumps(log.records)


@pytest.mark.parametrize("streaming", [False, True])
async def test_insufficient_budget_never_starts_another_attempt(streaming):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("failed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            await invoke(
                AuthorizedHttp(client, OAuth()),
                streaming,
                RequestPolicy(deadline=time.monotonic() + 0.04),
            )
    assert len(calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("side_effecting", [False, True])
async def test_cancellation_during_backoff_stops_retries(streaming, side_effecting):
    started = asyncio.Event()
    calls = []
    log = Log()

    def handler(request):
        calls.append(request)
        started.set()
        raise httpx.ConnectError("failed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wire = AuthorizedHttp(client, OAuth(), diagnostic_logging=True, diagnostic_logger=log)
        task = asyncio.create_task(
            invoke(
                wire,
                streaming,
                RequestPolicy(deadline=time.monotonic() + 3, side_effecting=side_effecting),
            )
        )
        await started.wait()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(OutcomeUnknown if side_effecting else asyncio.CancelledError):
            await task
    assert len(calls) == 1
    assert log.records[0]["phase"] == "retry_backoff"
    assert log.records[0]["termination_reason"] == "external_cancel"


@pytest.mark.parametrize("streaming", [False, True])
async def test_http_error_diagnostic_is_bounded_and_allowlisted(streaming):
    log = Log()
    response = {
        "error": {
            "code": "invalid_argument",
            "param": "tools",
            "message": "SECRET_PROMPT",
            "token": "SECRET_TOKEN",
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json=response))
    ) as client:
        with pytest.raises(ProtocolError):
            await invoke(
                AuthorizedHttp(client, OAuth(), diagnostic_logging=True, diagnostic_logger=log),
                streaming,
                RequestPolicy(),
            )
    record = log.records[0]
    assert record["status"] == 400
    assert record["request_kind"] == "chat"
    assert record["upstream_error_code"] == "invalid_argument"
    assert record["upstream_error_param"] == "tools"
    assert record["error_body_state"] == "json"
    assert len(record["trace_id"]) == 32
    assert "SECRET_" not in json.dumps(record)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("body_kind", ["oversized", "slow", "broken", "deep", "unexpected"])
async def test_diagnostic_body_failure_does_not_change_known_http_error(streaming, body_kind):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if body_kind == "oversized":
                yield b"x" * 9000
            elif body_kind == "deep":
                yield b"[" * 1500 + b"]" * 1500
            elif body_kind == "slow":
                await asyncio.Event().wait()
            elif body_kind == "unexpected":
                raise ValueError("SECRET_ERROR")
            else:
                raise httpx.ReadError("SECRET_ERROR")

    log = Log()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, stream=Body()))
    ) as client:
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(
                invoke(
                    AuthorizedHttp(client, OAuth(), diagnostic_logging=True, diagnostic_logger=log),
                    streaming,
                    RequestPolicy(deadline=time.monotonic() + 2, side_effecting=True),
                ),
                0.8,
            )
    assert log.records[0]["status"] == 400
    expected = (
        {"too_large"}
        if body_kind == "oversized"
        else {"json", "unparseable"}
        if body_kind == "deep"
        else {"read_failed"}
    )
    assert log.records[0]["error_body_state"] in expected
    assert "SECRET_" not in json.dumps(log.records)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("error_type", [httpx.RemoteProtocolError, httpx.ReadError])
async def test_unknown_submission_never_retries(streaming, error_type):
    calls = []

    def handler(request):
        calls.append(request)
        raise error_type("SECRET_ERROR", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OutcomeUnknown):
            await invoke(
                AuthorizedHttp(client, OAuth()),
                streaming,
                RequestPolicy(side_effecting=True, safe_pre_send_retries=3),
            )
    assert len(calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_rate_limit_is_not_retried(streaming):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RateLimited):
            await invoke(AuthorizedHttp(client, OAuth()), streaming, RequestPolicy())
    assert len(calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_account_rebind_during_connection_retry_stops_old_request(streaming):
    from grok_oauth.errors import AuthorizationChanged

    class SwitchingOAuth(OAuth):
        binding_generation = 0

    oauth = SwitchingOAuth()
    calls = []

    def handler(request):
        calls.append(request)
        oauth.binding_generation += 1
        raise httpx.ConnectError("connection failed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthorizationChanged):
            await invoke(
                AuthorizedHttp(client, oauth),
                streaming,
                RequestPolicy(deadline=time.monotonic() + 2),
            )
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["dns", "tls", "proxy", "connect_timeout"])
async def test_connection_category_uses_exception_types_not_private_message(kind):
    import socket
    import ssl

    error = httpx.ConnectError("SECRET")
    if kind == "dns":
        error.__cause__ = socket.gaierror(-2, "SECRET_HOST")
    elif kind == "tls":
        error.__cause__ = ssl.SSLError("SECRET_CERT")
    elif kind == "proxy":
        error = httpx.ProxyError("SECRET_PROXY")
    else:
        error = httpx.ConnectTimeout("SECRET_TIMEOUT")
    log = Log()

    def handler(request):
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            await invoke(
                AuthorizedHttp(client, OAuth(), diagnostic_logging=True, diagnostic_logger=log),
                False,
                RequestPolicy(safe_pre_send_retries=0),
            )
    assert log.records[0]["connection_error_kind"] == kind
    assert "SECRET" not in json.dumps(log.records)


@pytest.mark.parametrize("streaming", [False, True])
async def test_error_json_parser_failure_keeps_known_http_status(monkeypatch, streaming):
    import grok_oauth.http as module

    body = b'{"error":{"code":"invalid_argument"}}'
    original = json.loads

    def parse(value, *args, **kwargs):
        if value == body:
            raise RecursionError("untrusted body nesting")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(module.json_module, "loads", parse)
    log = Log()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, content=body))
    ) as client:
        with pytest.raises(ProtocolError):
            await invoke(
                AuthorizedHttp(client, OAuth(), diagnostic_logging=True, diagnostic_logger=log),
                streaming,
                RequestPolicy(),
            )
    assert log.records[0]["status"] == 400
    assert log.records[0]["error_body_state"] == "unparseable"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("side_effecting", [False, True])
async def test_cancel_during_error_body_preserves_submission_semantics(streaming, side_effecting):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    log = Log()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, stream=Body()))
    ) as client:
        wire = AuthorizedHttp(client, OAuth(), diagnostic_logging=True, diagnostic_logger=log)
        task = asyncio.create_task(
            invoke(
                wire,
                streaming,
                RequestPolicy(deadline=time.monotonic() + 3, side_effecting=side_effecting),
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(OutcomeUnknown if side_effecting else asyncio.CancelledError):
            await task
    assert closed.is_set()
    assert log.records[0]["termination_reason"] == "external_cancel"
    assert log.records[0]["phase"] == "error_body"
