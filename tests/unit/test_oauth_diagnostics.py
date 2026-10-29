import asyncio
import json
import socket
import time

import httpx
import pytest

from grok_oauth.errors import ProtocolError
from grok_oauth.models import DeviceFlow, TokenSnapshot
from grok_oauth.oauth import OAuthWireClient


class Logger:
    def __init__(self):
        self.records = []

    def info(self, template, value):
        assert template == "grok_oauth_transport_diag %s"
        self.records.append(json.loads(value))


def make_wire(client, logger=None, **kwargs):
    return OAuthWireClient(client, client_id="c", scope="s", diagnostic_logger=logger, **kwargs)


@pytest.mark.asyncio
async def test_oauth_start_failure_has_safe_details_without_enabled_logging():
    async def handler(request):
        try:
            raise socket.gaierror("TOKEN_SECRET host-name")
        except OSError as exc:
            raise httpx.ConnectError("TOKEN_SECRET", request=request) from exc

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wire = make_wire(client)
        with pytest.raises(ProtocolError) as caught:
            await wire.start_device_flow(owner_id="admin", epoch=0)
    detail = caught.value.oauth_diagnostic
    assert detail["oauth_stage"] == "discovery" and detail["oauth_reason"] == "dns"
    assert detail["cause"] == "ConnectError" and len(detail["trace_id"]) == 32
    assert "TOKEN_SECRET" not in str(detail) and "host-name" not in str(detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,reason", [(b"<html>SECRET</html>", "invalid_json"), (b"[]", "invalid_response")]
)
async def test_oauth_refresh_parse_failure_is_diagnosed_without_raw_response(body, reason):
    logger = Logger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ) as client:
        wire = make_wire(client, logger, diagnostic_logging=True)
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(ProtocolError) as caught:
            await wire.refresh(TokenSnapshot("default", "SECRET", "SECRET", None, "s", "c"))
    detail = caught.value.oauth_diagnostic
    assert detail["oauth_stage"] == "token_refresh" and detail["oauth_reason"] == reason
    assert detail["status"] == 200 and logger.records[-1]["outcome"] == "failure"
    assert "SECRET" not in str(logger.records)


@pytest.mark.asyncio
async def test_oauth_poll_retries_report_then_success_and_keep_flow_identity():
    now, requests = 0.0, 0
    logger, snapshots = Logger(), []

    async def sleep(delay):
        nonlocal now
        now += delay

    def handler(request):
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("SECRET", request=request)
        return httpx.Response(200, json={"access_token": "SECRET", "refresh_token": "SECRET"})

    flow = DeviceFlow("f", "admin", "u", "https://auth.x.ai/device", "SECRET", 60, 5, 0, 60, 60)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wire = make_wire(
            client,
            logger,
            diagnostic_logging=True,
            flow_diagnostic=lambda f, d: snapshots.append((f, d)),
        )
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        result = await wire.poll_device_flow(flow, clock=lambda: now, sleep=sleep)
    assert result.access_token == "SECRET" and requests == 2
    retry = next(d for d in logger.records if d["outcome"] == "retrying")
    assert retry["oauth_reason"] == "connect" and retry["retry_in_ms"] == 10000
    assert retry["poll_retries"] == 1
    assert logger.records[-1]["outcome"] == "success" and logger.records[-1]["oauth_reason"] == ""
    assert len({d["trace_id"] for d in logger.records}) == 1
    assert all(f is flow for f, _ in snapshots) and "SECRET" not in str(logger.records)


@pytest.mark.asyncio
async def test_oauth_cancel_is_propagated_and_diagnostic_callback_failures_are_isolated():
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Future()

    class Broken:
        def info(self, *args):
            raise RuntimeError("SECRET")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wire = make_wire(client, Broken(), diagnostic_logging=True)
        task = asyncio.create_task(wire.start_device_flow(owner_id="admin", epoch=0))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_oauth_http_failure_classifies_status_without_body_or_header_leak():
    logger = Logger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                403, json={"error": "SECRET"}, headers={"x-request-id": "SECRET"}
            )
        )
    ) as client:
        wire = make_wire(client, logger, diagnostic_logging=True)
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(ProtocolError) as caught:
            await wire.refresh(TokenSnapshot("default", "SECRET", "SECRET", None, "s", "c"))
    assert caught.value.oauth_diagnostic["oauth_reason"] == "http_status"
    assert caught.value.oauth_diagnostic["status"] == 403 and "SECRET" not in str(logger.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "refresh"])
async def test_oauth_operation_deadline_is_timeout_without_changing_public_error(operation):
    logger = Logger()

    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        wire = make_wire(client, logger, diagnostic_logging=True, operation_timeout=0.01)
        with pytest.raises(ProtocolError) as caught:
            if operation == "start":
                await wire.start_device_flow(owner_id="admin", epoch=0)
            else:
                wire._token_endpoint = "https://auth.x.ai/oauth2/token"
                await wire.refresh(TokenSnapshot("default", "a", "r", None, "s", "c"))
    assert caught.value.oauth_diagnostic["oauth_reason"] == "deadline"
    assert logger.records[-1]["outcome"] == "timeout"
    assert logger.records[-1]["termination_reason"] == "request_deadline"


@pytest.mark.asyncio
async def test_oauth_success_is_preserved_when_both_diagnostic_outputs_fail():
    class Broken:
        def info(self, *args):
            raise RuntimeError("SECRET")

    def broken_callback(*args):
        raise RuntimeError("SECRET")

    flow = DeviceFlow(
        "f",
        "admin",
        "u",
        "https://auth.x.ai/device",
        "d",
        60,
        5,
        0,
        time.monotonic() + 60,
        time.time() + 60,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "a"}))
    ) as client:
        wire = make_wire(client, Broken(), diagnostic_logging=True, flow_diagnostic=broken_callback)
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        result = await wire.poll_device_flow(flow)
    assert result.access_token == "a"


@pytest.mark.asyncio
async def test_cancelled_pending_poll_reports_cancellation_and_preserves_exception():
    logger, sleeping = Logger(), asyncio.Event()

    async def sleep(_):
        sleeping.set()
        await asyncio.Future()

    flow = DeviceFlow(
        "f",
        "admin",
        "u",
        "https://auth.x.ai/device",
        "d",
        60,
        5,
        0,
        time.monotonic() + 60,
        time.time() + 60,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": "authorization_pending"})
        )
    ) as client:
        wire = make_wire(client, logger, diagnostic_logging=True)
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        task = asyncio.create_task(wire.poll_device_flow(flow, sleep=sleep))
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert logger.records[0]["outcome"] == "pending"
    assert logger.records[-1]["outcome"] == "cancelled"
    assert logger.records[-1]["oauth_reason"] == "cancelled"
    assert logger.records[-1]["termination_reason"] == "external_cancel"
