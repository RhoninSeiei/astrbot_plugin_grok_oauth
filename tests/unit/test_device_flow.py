import asyncio
import base64
import json
import math
import time
from dataclasses import replace

import httpx
import pytest

from grok_oauth.errors import (
    AuthorizationDenied,
    DeviceCodeExpired,
    ProtocolError,
    ReauthorizationRequired,
)
from grok_oauth.models import DeviceFlow, TokenSnapshot
from grok_oauth.oauth import OAuthWireClient


def discovery():
    return {
        "issuer": "https://auth.x.ai",
        "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }


@pytest.mark.asyncio
async def test_pending_and_slow_down_follow_server_interval():
    seconds = 0.0
    waits = []
    polls = 0

    async def sleep(delay):
        nonlocal seconds
        waits.append(delay)
        seconds += delay

    def handler(request):
        nonlocal polls
        assert request.url.host == "auth.x.ai" and request.headers["user-agent"].startswith(
            "astrbot-plugin-grok-oauth/"
        )
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        if request.url.path.endswith("device/code"):
            assert "referrer=astrbot_plugin_grok_oauth" in request.content.decode()
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD",
                    "verification_uri": "https://auth.x.ai/device",
                    "expires_in": 120,
                    "interval": 5,
                },
            )
        polls += 1
        if polls == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        if polls == 2:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "api:access",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        wire = OAuthWireClient(http, client_id="client", scope="api:access")
        flow = await wire.start_device_flow(owner_id="admin", epoch=3)
        flow = replace(flow, deadline=seconds + flow.expires_in)
        assert "device-secret" not in repr(flow) and flow.owner_id == "admin" and flow.epoch == 3
        result = await wire.poll_device_flow(flow, clock=lambda: seconds, sleep=sleep)
    assert polls == 3 and waits[-2] >= 5 and waits[-1] >= 10 and result.access_token == "access"


@pytest.mark.asyncio
@pytest.mark.parametrize("expires,interval", [(float("nan"), -1), ("120", "5"), (None, None)])
async def test_invalid_timing_fields_use_finite_defaults(expires, interval):
    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        if isinstance(expires, float) and math.isnan(expires):
            return httpx.Response(
                200,
                content=b'{"device_code":"d","user_code":"u","verification_uri":"https://auth.x.ai/device","expires_in":NaN,"interval":-1}',
            )
        return httpx.Response(
            200,
            json={
                "device_code": "d",
                "user_code": "u",
                "verification_uri": "https://auth.x.ai/device",
                "expires_in": expires,
                "interval": interval,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        flow = await OAuthWireClient(http, client_id="c", scope="s").start_device_flow(
            owner_id="o", epoch=0
        )
    assert (
        math.isfinite(flow.expires_in)
        and flow.expires_in > 0
        and math.isfinite(flow.interval)
        and flow.interval >= 1
    )


@pytest.mark.asyncio
async def test_foreign_discovery_endpoint_rejected_before_secret_post():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={**discovery(), "token_endpoint": "https://evil.example/token"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        with pytest.raises(ProtocolError):
            await OAuthWireClient(http, client_id="c", scope="s").start_device_flow(
                owner_id="o", epoch=0
            )
    assert len(requests) == 1 and requests[0].method == "GET"


@pytest.mark.asyncio
async def test_terminal_flow_and_refresh_errors_are_typed():
    responses = iter(
        [
            httpx.Response(400, json={"error": "authorization_denied"}),
            httpx.Response(400, json={"error": "expired_token"}),
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        flow = DeviceFlow("f", "o", "u", "https://auth.x.ai/device", "d", 10, 1, 0, 10, 10**12)
        with pytest.raises(AuthorizationDenied):
            await wire.poll_device_flow(flow, clock=lambda: 0, sleep=lambda _: None)
        with pytest.raises(DeviceCodeExpired):
            await wire.poll_device_flow(flow, clock=lambda: 0, sleep=lambda _: None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "secret"}
            )
        )
    ) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(ReauthorizationRequired) as caught:
            await wire.refresh(TokenSnapshot("default", "a", "r", None, "s", "c", 1, 0))
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_port_is_typed_without_leaking_upstream_value():
    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        return httpx.Response(
            200,
            json={
                "device_code": "d",
                "user_code": "u",
                "verification_uri": "https://auth.x.ai:SECRET_SENTINEL/device",
                "expires_in": 60,
                "interval": 5,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProtocolError) as caught:
            await OAuthWireClient(http, client_id="c", scope="s").start_device_flow(
                owner_id="o", epoch=0
            )
    assert "SECRET_SENTINEL" not in str(caught.value)


@pytest.mark.asyncio
async def test_poll_deadline_covers_discovery_and_token_response():
    async def slow_discovery(request):
        await asyncio.sleep(0.032)
        return httpx.Response(200, json=discovery())

    flow = DeviceFlow(
        "f",
        "o",
        "u",
        "https://auth.x.ai/device",
        "d",
        60,
        1,
        0,
        time.monotonic() + 0.01,
        time.time() + 60,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_discovery)) as http:
        with pytest.raises(DeviceCodeExpired):
            await OAuthWireClient(http, client_id="c", scope="s").poll_device_flow(flow)

    async def slow_token(request):
        await asyncio.sleep(0.032)
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})

    flow = replace(flow, deadline=time.monotonic() + 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_token)) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(DeviceCodeExpired):
            await wire.poll_device_flow(flow)

    async def pending(request):
        return httpx.Response(400, json={"error": "authorization_pending"})

    async def slow_sleep(delay):
        await asyncio.sleep(0.032)

    flow = replace(flow, deadline=time.monotonic() + 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(pending)) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(DeviceCodeExpired):
            await wire.poll_device_flow(flow, sleep=slow_sleep)


@pytest.mark.asyncio
async def test_oauth_json_body_is_stopped_at_wire_limit():
    class Oversized(httpx.AsyncByteStream):
        def __init__(self):
            self.yielded = 0

        async def __aiter__(self):
            for _ in range(2048):
                self.yielded += 1
                yield b"x" * 1024

    oversized = Oversized()

    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        return httpx.Response(200, stream=oversized)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProtocolError):
            await OAuthWireClient(http, client_id="c", scope="s").start_device_flow(
                owner_id="o", epoch=0
            )
    assert oversized.yielded < 2048


@pytest.mark.asyncio
async def test_malformed_numeric_error_and_jwt_fields_are_safe():
    huge = 10**1000
    responses = iter(
        [
            httpx.Response(200, json={"access_token": "a.W10.b", "refresh_token": "r"}),
            httpx.Response(400, json={"error": ["invalid_grant"]}),
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        result = await wire.refresh(TokenSnapshot("default", "a", "r", None, "s", "c", 1, 0))
        assert result.expires_at is None
        with pytest.raises(ProtocolError):
            await wire.refresh(TokenSnapshot("default", "a", "r", None, "s", "c", 1, 0))

    def huge_handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        return httpx.Response(
            200,
            content=(
                '{"device_code":"d","user_code":"u","verification_uri":'
                '"https://auth.x.ai/device","expires_in":' + str(huge) + "}"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(huge_handler)) as http:
        flow = await OAuthWireClient(http, client_id="c", scope="s").start_device_flow(
            owner_id="o", epoch=0
        )
    assert math.isfinite(flow.expires_in) and flow.expires_in == 300

    jwt_payload = base64.urlsafe_b64encode(json.dumps({"exp": huge}).encode()).decode().rstrip("=")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"access_token": f"a.{jwt_payload}.b", "refresh_token": "r"}
            )
        )
    ) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        result = await wire.refresh(TokenSnapshot("default", "a", "r", None, "s", "c", 1, 0))
    assert result.expires_at is None


@pytest.mark.asyncio
async def test_start_and_refresh_have_non_extendable_operation_budget():
    async def slow_discovery(request):
        await asyncio.sleep(0.032)
        return httpx.Response(200, json=discovery())

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_discovery)) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s", operation_timeout=0.01)
        with pytest.raises(ProtocolError):
            await wire.start_device_flow(owner_id="o", epoch=0)

    class SlowRefreshBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"access_token":"new",'
            await asyncio.sleep(0.032)
            yield b'"refresh_token":"r"}'

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowRefreshBody()))
    ) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s", operation_timeout=0.01)
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(ProtocolError):
            await wire.refresh(TokenSnapshot("default", "a", "r", None, "s", "c", 1, 0))

    with pytest.raises(ValueError):
        OAuthWireClient(http, client_id="c", scope="s", operation_timeout=31)


@pytest.mark.asyncio
async def test_poll_non_string_error_is_typed():
    flow = DeviceFlow(
        "f",
        "o",
        "u",
        "https://auth.x.ai/device",
        "d",
        60,
        1,
        0,
        time.monotonic() + 1,
        time.time() + 60,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json={"error": []}))
    ) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(ProtocolError):
            await wire.poll_device_flow(flow)


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["auth.x.ai", "accounts.x.ai"])
async def test_browser_verification_hosts_keep_token_destination_pinned(host):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        return httpx.Response(
            200,
            json={
                "device_code": "synthetic-device",
                "user_code": "TEST-CODE",
                "verification_uri": f"https://{host}/device",
                "verification_uri_complete": f"https://{host}/device?user_code=TEST-CODE",
                "expires_in": 300,
                "interval": 5,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        flow = await OAuthWireClient(http, client_id="c", scope="s").start_device_flow(
            owner_id="o", epoch=0
        )
    assert flow.verification_uri == f"https://{host}/device"
    assert all(request.url.host == "auth.x.ai" for request in requests)


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.x.ai.evil.example/device",
        "https://evil.example/device",
        "http://accounts.x.ai/device",
        "https://user:password@accounts.x.ai/device",
        "https://accounts.x.ai:8443/device",
        "https://accounts.x.ai/device#fragment",
    ],
)
def test_browser_verification_rejects_untrusted_destinations(url):
    from grok_oauth.oauth import _safe_auth_url

    with pytest.raises(ProtocolError):
        _safe_auth_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ConnectTimeout, httpx.ConnectError])
async def test_poll_connect_failure_backs_off_and_preserves_device_flow(failure):
    now = 0.0
    waits, requests = [], []

    async def sleep(delay):
        nonlocal now
        waits.append(delay)
        now += delay

    def handler(request):
        requests.append(request)
        assert request.extensions["timeout"]["connect"] <= 30
        if len(requests) < 3:
            raise failure("NETWORK_SECRET_SENTINEL", request=request)
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})

    flow = DeviceFlow(
        "f", "o", "u", "https://accounts.x.ai/device", "same-device", 100, 5, 0, 100, 100
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wire = OAuthWireClient(client, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        result = await wire.poll_device_flow(flow, clock=lambda: now, sleep=sleep)
    assert result.access_token == "a" and waits == [10, 20]
    assert len({request.content for request in requests}) == 1


@pytest.mark.asyncio
async def test_poll_connect_failure_cannot_extend_device_deadline():
    now = 0.0
    requests, waits = [], []

    async def sleep(delay):
        nonlocal now
        waits.append(delay)
        now += delay

    def handler(request):
        requests.append(request)
        raise httpx.ConnectTimeout("unreachable", request=request)

    flow = DeviceFlow("f", "o", "u", "https://accounts.x.ai/device", "d", 15, 5, 0, 15, 15)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wire = OAuthWireClient(client, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(DeviceCodeExpired):
            await wire.poll_device_flow(flow, clock=lambda: now, sleep=sleep)
    assert len(requests) == 2 and waits == [10, 5]


@pytest.mark.asyncio
async def test_poll_read_failure_remains_terminal_without_redeeming_twice():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadError("SECRET_SENTINEL", request=request)

    flow = DeviceFlow(
        "f",
        "o",
        "u",
        "https://accounts.x.ai/device",
        "d",
        60,
        5,
        0,
        time.monotonic() + 60,
        time.time() + 60,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wire = OAuthWireClient(client, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        with pytest.raises(ProtocolError) as error:
            await wire.poll_device_flow(flow)
    assert len(requests) == 1 and "SECRET_SENTINEL" not in str(error.value)


@pytest.mark.asyncio
async def test_poll_connect_backoff_is_cancellable():
    entered = asyncio.Event()

    async def sleep(delay):
        entered.set()
        await asyncio.Event().wait()

    def handler(request):
        raise httpx.ConnectTimeout("unreachable", request=request)

    flow = DeviceFlow(
        "f",
        "o",
        "u",
        "https://accounts.x.ai/device",
        "d",
        60,
        5,
        0,
        time.monotonic() + 60,
        time.time() + 60,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        wire = OAuthWireClient(client, client_id="c", scope="s")
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        task = asyncio.create_task(wire.poll_device_flow(flow, sleep=sleep))
        try:
            await asyncio.wait_for(entered.wait(), 0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
