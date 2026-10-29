import asyncio
import time
from dataclasses import replace

import httpx
import pytest

from grok_oauth.credentials import OAuthService
from grok_oauth.errors import (
    AuthorizationChanged,
    CredentialPersistenceError,
    ReauthorizationRequired,
    ServiceClosed,
)
from grok_oauth.models import TokenSnapshot
from grok_oauth.oauth import OAuthWireClient
from grok_oauth.token_store import TokenStore


def sample(**changes):
    fields = {
        "slot": "default",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": 0.0,
        "scope": "api:access",
        "client_id": "client",
        "version": 1,
        "epoch": 0,
    }
    fields.update(changes)
    return TokenSnapshot(**fields)


async def service_with(tmp_path, refresh):
    store = TokenStore(tmp_path / "credentials.json")
    await store.open()
    await store.commit(sample())
    service = OAuthService(store, refresh, clock=lambda: 100.0)
    await service.open()
    return store, service


@pytest.mark.asyncio
async def test_one_refresh_for_one_hundred_waiters(tmp_path):
    calls = 0

    async def refresh(current):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return replace(current, access_token="new", refresh_token="rotated", expires_at=10_000.0)

    store, service = await service_with(tmp_path, refresh)
    results = await asyncio.gather(*(service.get_token() for _ in range(100)))
    assert (
        calls == 1
        and {r.version for r in results} == {2}
        and {r.access_token for r in results} == {"new"}
    )
    assert (await store.load()).refresh_token == "rotated"
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_refresh(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def refresh(current):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return replace(current, access_token="new", expires_at=1000.0)

    _, service = await service_with(tmp_path, refresh)
    cancelled = asyncio.create_task(service.get_token())
    survivor = asyncio.create_task(service.get_token())
    await started.wait()
    cancelled.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert (await survivor).access_token == "new" and calls == 1
    await service.close()


@pytest.mark.asyncio
async def test_missing_refresh_is_retained_and_unknown_expiry_is_usable(tmp_path):
    async def refresh(current):
        return replace(current, access_token="new", refresh_token="", expires_at=None)

    _, service = await service_with(tmp_path, refresh)
    result = await service.get_token(force_refresh=True)
    assert result.refresh_token == "old-refresh" and result.expires_at is None
    assert (await service.get_token()).access_token == "new"
    await service.close()


@pytest.mark.asyncio
async def test_persistence_failure_blocks_old_token_reuse(tmp_path, monkeypatch):
    calls = 0

    async def refresh(current):
        nonlocal calls
        calls += 1
        return replace(current, access_token="new", refresh_token="rotated")

    store, service = await service_with(tmp_path, refresh)

    async def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(store, "commit", fail)
    with pytest.raises(CredentialPersistenceError):
        await service.get_token(force_refresh=True)
    with pytest.raises(CredentialPersistenceError):
        await service.get_token(force_refresh=True)
    assert calls == 1 and service.snapshot() is None
    assert await store.load() is None
    await service.close()


@pytest.mark.asyncio
async def test_disconnect_invalidates_late_refresh(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh(current):
        started.set()
        await release.wait()
        return replace(current, access_token="late")

    store, service = await service_with(tmp_path, refresh)
    pending = asyncio.create_task(service.get_token(force_refresh=True))
    await started.wait()
    await service.disconnect()
    release.set()
    with pytest.raises(AuthorizationChanged):
        await pending
    assert service.snapshot() is None and await store.load() is None
    await service.close()


@pytest.mark.asyncio
async def test_invalid_grant_terminal_and_late_bind_rejected(tmp_path):
    calls = 0

    async def refresh(_):
        nonlocal calls
        calls += 1
        raise ReauthorizationRequired()

    store, service = await service_with(tmp_path, refresh)
    with pytest.raises(ReauthorizationRequired):
        await service.get_token(force_refresh=True)
    with pytest.raises(ReauthorizationRequired):
        await service.get_token(force_refresh=True)
    assert calls == 1
    assert await store.load() is None
    epoch = service.epoch
    await service.disconnect()
    with pytest.raises(AuthorizationChanged):
        await service.bind(sample(), expected_epoch=epoch)
    await service.close()
    with pytest.raises(ServiceClosed):
        await service.bind(sample(), expected_epoch=service.epoch)


@pytest.mark.asyncio
async def test_cancellation_waits_for_bind_commit_before_returning(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "credentials.json")
    service = OAuthService(store, None)
    await service.open()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = store.commit

    async def paused(value):
        entered.set()
        await release.wait()
        await original(value)

    monkeypatch.setattr(store, "commit", paused)
    binding = asyncio.create_task(service.bind(sample(), expected_epoch=service.epoch))
    await entered.wait()
    binding.cancel()
    await asyncio.sleep(0)
    assert not binding.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await binding
    assert service.snapshot() is not None and (await store.load()) == service.snapshot()
    await service.close()


@pytest.mark.asyncio
async def test_disconnect_retires_old_epoch_refresh_before_rebind(tmp_path):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def refresh(current):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
            return replace(current, access_token="generation-one-late", expires_at=1000.0)
        return replace(current, access_token="generation-two", expires_at=1000.0)

    _, service = await service_with(tmp_path, refresh)
    old_waiter = asyncio.create_task(service.get_token(force_refresh=True))
    await first_started.wait()
    await service.disconnect()
    await service.bind(sample(), expected_epoch=service.epoch)

    async def release_later():
        await asyncio.sleep(0.02)
        release_first.set()

    releaser = asyncio.create_task(release_later())
    new_result = await service.get_token(force_refresh=True)
    await releaser
    old_result = (await asyncio.gather(old_waiter, return_exceptions=True))[0]

    assert calls == 2
    assert new_result.access_token == "generation-two"
    assert isinstance(old_result, AuthorizationChanged)
    await service.close()


@pytest.mark.asyncio
async def test_close_waits_for_refresh_to_finish_before_returning(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh(current):
        started.set()
        await release.wait()
        return replace(current, access_token="late")

    _, service = await service_with(tmp_path, refresh)
    waiter = asyncio.create_task(service.get_token(force_refresh=True))
    await started.wait()
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    returned_before_refresh = closing.done()
    release.set()
    await closing
    await asyncio.gather(waiter, return_exceptions=True)

    assert not returned_before_refresh
    assert service.status == "closed"


@pytest.mark.asyncio
async def test_refresh_budget_allows_close_and_releases_store_lock(tmp_path):
    body_started = asyncio.Event()

    class SlowTokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            body_started.set()
            yield b'{"access_token":"new",'
            await asyncio.sleep(0.2)
            yield b'"refresh_token":"rotated"}'

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowTokenBody()))
    ) as http:
        wire = OAuthWireClient(http, client_id="c", scope="s", operation_timeout=0.01)
        wire._token_endpoint = "https://auth.x.ai/oauth2/token"
        store, service = await service_with(tmp_path, wire.refresh)
        refreshing = asyncio.create_task(service.get_token(force_refresh=True))
        await body_started.wait()
        started = time.monotonic()
        await asyncio.wait_for(service.close(), timeout=0.15)
        elapsed = time.monotonic() - started
        result = (await asyncio.gather(refreshing, return_exceptions=True))[0]

    assert elapsed < 0.15
    assert isinstance(result, Exception)
    replacement = TokenStore(tmp_path / "credentials.json")
    await replacement.open()
    await replacement.close()
