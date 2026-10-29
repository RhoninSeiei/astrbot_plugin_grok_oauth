import asyncio
import importlib.util
from dataclasses import replace

import httpx
import pytest

from grok_oauth.credentials import OAuthService
from grok_oauth.models import TokenSnapshot
from grok_oauth.token_store import TokenStore

BODY = {
    "config": {
        "creditUsagePercent": 25,
        "productUsage": [{"product": "GrokChat", "usagePercent": 25}],
        "isUnifiedBillingUser": True,
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-09-19T00:00:00Z",
            "end": "2026-09-26T00:00:00Z",
        },
    }
}


async def setup(tmp_path, handler, *, refresh=None, clock=None):
    assert importlib.util.find_spec("grok_oauth.billing") is not None, (
        "BillingClient is not implemented"
    )
    from grok_oauth.billing import BillingClient

    oauth = OAuthService(TokenStore(tmp_path / "credentials.json"), refresh)
    await oauth.open()
    await oauth.bind(
        TokenSnapshot(
            "default", "test-access", "test-refresh", None, "", "client", user_id="user-a"
        ),
        expected_epoch=0,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    billing = BillingClient(http, oauth, **({"clock": clock} if clock else {}))
    return oauth, http, billing


async def close(oauth, http, billing):
    await billing.close()
    await oauth.close()
    await http.aclose()


@pytest.mark.asyncio
async def test_fixed_target_headers_cache_and_force_refresh(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer test-access"
        assert request.headers["x-userid"] == "user-a"
        assert request.headers["x-xai-token-auth"] == "xai-grok-cli"
        from grok_oauth.version import USER_AGENT

        assert request.headers["x-grok-client-version"] == USER_AGENT
        return httpx.Response(200, json=BODY)

    args = await setup(tmp_path, handler, clock=lambda: 1789948800.0)
    oauth, http, billing = args
    try:
        first = await billing.get_usage()
        assert first.used_percent == 25 and first.remaining_percent == 75 and not first.cached
        assert (await billing.get_usage()).cached
        assert len(requests) == 1
        assert not (await billing.get_usage(force_refresh=True)).cached
        assert len(requests) == 2
    finally:
        await close(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 403, 429, 500])
async def test_failures_do_not_follow_redirect_or_revoke_chat(tmp_path, status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"Location": "https://evil.invalid/", "Retry-After": "120"},
            text="private-body",
        )

    args = await setup(tmp_path, handler)
    oauth, http, billing = args
    try:
        result = await billing.get_usage()
        assert result.status != "success" and result.used_percent is None
        assert "private-body" not in repr(result)
        assert oauth.status == "authorized" and calls == 1
        if status == 429:
            assert (await billing.get_usage(force_refresh=True)).status == "rate_limited"
            assert calls == 1
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_401_refresh_once_with_rejected_version(tmp_path):
    calls = []
    versions = []

    async def refresh(previous):
        versions.append(previous.version)
        return replace(previous, access_token="renewed")

    def handler(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(401) if len(calls) == 1 else httpx.Response(200, json=BODY)

    args = await setup(tmp_path, handler, refresh=refresh)
    try:
        assert (await args[2].get_usage()).status == "success"
        assert calls == ["Bearer test-access", "Bearer renewed"] and versions == [1]
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_concurrent_waiter_cancellation_and_rebind_discard(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json=BODY)

    args = await setup(tmp_path, handler)
    oauth, http, billing = args
    try:
        one = asyncio.create_task(billing.get_usage())
        two = asyncio.create_task(billing.get_usage())
        await entered.wait()
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        release.set()
        assert (await two).status == "success" and calls == 1
        release.clear()
        entered.clear()
        old = asyncio.create_task(billing.get_usage(force_refresh=True))
        await entered.wait()
        await oauth.bind(replace(oauth.snapshot(), user_id="user-b"), expected_epoch=oauth.epoch)
        release.set()
        assert (await old).status == "authorization_changed"
        assert not (await billing.get_usage()).cached and calls == 3
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_stale_expiry_period_and_forbidden(tmp_path):
    now = [1789948800.0]
    status = [200]

    def handler(request):
        return httpx.Response(status[0], json=BODY)

    args = await setup(tmp_path, handler, clock=lambda: now[0])
    try:
        billing = args[2]
        assert (await billing.get_usage()).status == "success"
        now[0] += 121
        status[0] = 500
        stale = await billing.get_usage()
        assert (
            stale.stale
            and stale.cached
            and stale.status == "unavailable"
            and stale.used_percent == 25
        )
        view = stale.to_breakdown_dict()
        assert view["stale"] and view["status"] == "unavailable"
        assert view["products"][0]["usage_percent"] == 25
        now[0] += 600
        expired = await billing.get_usage()
        assert expired.used_percent is None and expired.to_breakdown_dict()["products"] == []
        status[0] = 200
        await billing.get_usage(force_refresh=True)
        status[0] = 403
        denied = await billing.get_usage(force_refresh=True)
        assert denied.used_percent is None and not denied.stale
        assert denied.to_breakdown_dict()["products"] == []
        status[0] = 500
        assert (await billing.get_usage()).to_breakdown_dict()["products"] == []
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_close_cancels_and_joins_inflight(tmp_path):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    args = await setup(tmp_path, handler)
    try:
        waiter = asyncio.create_task(args[2].get_usage())
        await entered.wait()
        await args[2].close()
        assert cancelled.is_set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await args[2].get_usage()).status == "closed"
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_no_stale_crosses_known_period_end(tmp_path):
    from datetime import datetime

    end = datetime.fromisoformat("2026-09-26T00:00:00+00:00").timestamp()
    now = [end - 1]
    status = [200]
    args = await setup(
        tmp_path, lambda request: httpx.Response(status[0], json=BODY), clock=lambda: now[0]
    )
    try:
        assert (await args[2].get_usage()).status == "success"
        now[0] = end + 1
        status[0] = 500
        result = await args[2].get_usage()
        assert result.used_percent is None and not result.stale
        assert result.to_breakdown_dict()["products"] == []
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_second_401_does_not_loop_or_revoke_binding(tmp_path):
    calls, refreshes = [], []

    async def refresh(token):
        refreshes.append(1)
        return replace(token, access_token="new")

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    args = await setup(tmp_path, handler, refresh=refresh)
    try:
        assert (await args[2].get_usage()).status == "reauth_required"
        assert len(calls) == 2 and len(refreshes) == 1
        assert args[0].status == "authorized"
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_missing_identity_never_sends_billing_or_guesses_account(tmp_path):
    calls = []

    async def refresh(token):
        return replace(token, access_token="new")

    args = await setup(tmp_path, lambda request: calls.append(request), refresh=refresh)
    try:
        oauth, http, billing = args
        await oauth.bind(replace(oauth.snapshot(), user_id=None), expected_epoch=oauth.epoch)
        result = await billing.get_usage()
        assert result.status == "identity_unavailable" and calls == []
    finally:
        await close(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["http", "auth"])
async def test_request_and_total_deadlines_are_enforced(tmp_path, monkeypatch, phase):
    import grok_oauth.billing as module

    release = asyncio.Event()
    budgets = []
    real_timeout = asyncio.timeout

    def accelerated_timeout(seconds):
        budgets.append(seconds)
        return real_timeout(seconds / 1000)

    async def handler(request):
        await release.wait()
        return httpx.Response(200, json=BODY)

    async def refresh(token):
        await release.wait()
        return replace(token, expires_at=None)

    args = await setup(tmp_path, handler, refresh=refresh)
    try:
        if phase == "auth":
            await args[0].bind(replace(args[0].snapshot(), expires_at=0), expected_epoch=0)
        monkeypatch.setattr(module.asyncio, "timeout", accelerated_timeout)
        result = await args[2].get_usage()
        assert result.status == "unavailable"
        assert budgets == ([30, 15] if phase == "http" else [30])
    finally:
        release.set()
        await close(*args)


@pytest.mark.asyncio
async def test_old_account_429_cannot_cool_down_new_account(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request.headers["x-userid"])
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json=BODY)

    args = await setup(tmp_path, handler)
    try:
        task = asyncio.create_task(args[2].get_usage())
        await entered.wait()
        await args[0].bind(replace(args[0].snapshot(), user_id="user-b"), expected_epoch=0)
        release.set()
        assert (await task).status == "authorization_changed"
        assert (await args[2].get_usage()).status == "success"
        assert calls == ["user-a", "user-b"]
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_billing_401_reuses_concurrent_chat_refresh(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls, refreshes = [], []

    async def refresh(token):
        refreshes.append(token.version)
        return replace(token, access_token="advanced")

    async def handler(request):
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return httpx.Response(401)
        return httpx.Response(200, json=BODY)

    args = await setup(tmp_path, handler, refresh=refresh)
    try:
        task = asyncio.create_task(args[2].get_usage())
        await entered.wait()
        await args[0].get_token(force_refresh=True)
        release.set()
        assert (await task).status == "success"
        assert refreshes == [1] and calls == ["Bearer test-access", "Bearer advanced"]
    finally:
        await close(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_status", [200, 401])
async def test_existing_access_token_queries_billing_without_new_login(tmp_path, first_status):
    import base64
    import json

    claims = {
        "iss": "https://auth.x.ai",
        "client_id": "client",
        "sub": "user-a",
        "principal_type": "User",
        "principal_id": "user-a",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    access = "header." + encoded + ".signature"
    calls, refreshes = [], []

    async def refresh(previous):
        refreshes.append(previous.version)
        return replace(previous, access_token=access)

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + access
        assert request.headers["x-userid"] == "user-a"
        return httpx.Response(first_status if len(calls) == 1 else 200, json=BODY)

    args = await setup(tmp_path, handler, refresh=refresh)
    try:
        oauth, http, billing = args
        await oauth.bind(
            replace(oauth.snapshot(), access_token=access, user_id=None), expected_epoch=0
        )
        result = await billing.get_usage()
        assert result.status == "success" and result.used_percent == 25
        assert len(calls) == (2 if first_status == 401 else 1)
        assert len(refreshes) == int(first_status == 401)
    finally:
        await close(*args)


@pytest.mark.asyncio
async def test_401_refresh_without_trusted_identity_stops_before_second_billing(tmp_path):
    import base64
    import json

    claims = {
        "iss": "https://auth.x.ai",
        "client_id": "client",
        "sub": "user-a",
        "principal_type": "User",
        "principal_id": "user-a",
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    calls = []

    async def refresh(previous):
        return replace(previous, access_token="opaque-refreshed-token", user_id=None)

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    args = await setup(tmp_path, handler, refresh=refresh)
    try:
        oauth, http, billing = args
        await oauth.bind(
            replace(
                oauth.snapshot(), user_id=None, access_token="header." + payload + ".signature"
            ),
            expected_epoch=0,
        )
        assert (await billing.get_usage()).status == "identity_unavailable"
        assert len(calls) == 1
    finally:
        await close(*args)
