import base64
import json
from dataclasses import replace

import pytest

from grok_oauth.credentials import OAuthService
from grok_oauth.models import TokenSnapshot
from grok_oauth.oauth import _token_snapshot
from grok_oauth.token_store import TokenStore


def jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def test_device_identity_comes_from_direct_token_response():
    token = _token_snapshot(
        {"access_token": jwt({"sub": "wrong-account"}), "id_token": jwt({"sub": "trusted-user"})},
        client_id="client",
        epoch=0,
    )
    assert getattr(token, "user_id", None) == "trusted-user"
    assert "trusted-user" not in repr(token)


@pytest.mark.parametrize("claims", [{}, {"sub": ""}, {"sub": "u\r\nheader"}, {"sub": 42}])
def test_missing_or_invalid_identity_is_not_guessed(claims):
    token = _token_snapshot(
        {"access_token": jwt({"sub": "not-an-id-token"}), "id_token": jwt(claims)},
        client_id="client",
        epoch=0,
    )
    assert getattr(token, "user_id", None) is None


@pytest.mark.asyncio
async def test_store_accepts_old_schema_and_persists_new_private_identity(tmp_path):
    token = TokenSnapshot("default", "access", "refresh", None, "", "client")
    store = TokenStore(tmp_path / "credentials.json")
    await store.open()
    try:
        await store.commit(token)
        old = json.loads(store.path.read_text())
        old.pop("user_id", None)
        store.path.write_text(json.dumps(old))
        assert getattr(await store.load(), "user_id", None) is None
        assert "user_id" in TokenSnapshot.__dataclass_fields__
        identified = replace(token, user_id="user-a")
        await store.commit(identified)
        assert (await store.load()).user_id == "user-a"
        assert store.path.stat().st_mode & 0o777 == 0o600
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_binding_generation_changes_on_rebind_not_refresh(tmp_path):
    async def refresh(previous):
        return TokenSnapshot("default", "new", "new-refresh", None, "", "client")

    service = OAuthService(TokenStore(tmp_path / "credentials.json"), refresh)
    await service.open()
    try:
        assert hasattr(service, "binding_generation")
        token = TokenSnapshot("default", "access", "refresh", None, "", "client", user_id="user-a")
        await service.bind(token, expected_epoch=0)
        first = service.binding_generation
        renewed = await service.get_token(force_refresh=True)
        assert renewed.user_id == "user-a"
        assert service.binding_generation == first
        await service.bind(replace(token, user_id="user-b"), expected_epoch=0)
        assert service.binding_generation != first
        second = service.binding_generation
        await service.disconnect()
        assert service.binding_generation != second and service.snapshot() is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_retired_refresh_failure_cannot_clear_new_binding(tmp_path):
    import asyncio

    from grok_oauth.errors import ReauthorizationRequired

    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(previous):
        entered.set()
        await release.wait()
        raise ReauthorizationRequired()

    service = OAuthService(TokenStore(tmp_path / "credentials.json"), refresh)
    await service.open()
    try:
        token = TokenSnapshot("default", "old", "refresh", None, "", "client", user_id="old-user")
        await service.bind(token, expected_epoch=0)
        task = asyncio.create_task(service.get_token(force_refresh=True))
        await entered.wait()
        await service.bind(replace(token, access_token="new", user_id="new-user"), expected_epoch=0)
        release.set()
        with pytest.raises(ReauthorizationRequired):
            await task
        assert service.status == "authorized"
        assert service.snapshot().user_id == "new-user"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_fails", [False, True])
async def test_old_refresh_cannot_cross_rebind_after_invalid_grant(tmp_path, old_fails):
    import asyncio

    from grok_oauth.errors import AuthorizationChanged, ReauthorizationRequired

    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(previous):
        if previous.user_id == "A":
            entered.set()
            await release.wait()
            if not old_fails:
                return replace(previous, access_token="renewed-A")
        raise ReauthorizationRequired()

    service = OAuthService(TokenStore(tmp_path / "credentials.json"), refresh)
    await service.open()
    try:
        token = TokenSnapshot("default", "A", "refresh", None, "", "client", user_id="A")
        await service.bind(token, expected_epoch=0)
        old = asyncio.create_task(service.get_token(force_refresh=True))
        await entered.wait()
        await service.bind(replace(token, access_token="B", user_id="B"), expected_epoch=0)
        with pytest.raises(ReauthorizationRequired):
            await service.get_token(force_refresh=True)
        assert service.snapshot() is None
        await service.bind(replace(token, access_token="C", user_id="C"), expected_epoch=0)
        release.set()
        try:
            await old
        except (AuthorizationChanged, ReauthorizationRequired):
            pass
        assert service.status == "authorized" and service.snapshot().user_id == "C"
    finally:
        await service.close()


@pytest.mark.parametrize("camel", [False, True])
def test_new_authorization_reuses_user_access_token_identity(camel):
    claims = {
        "iss": "https://auth.x.ai",
        "client_id": "client",
        "sub": "user-a",
        "principalType" if camel else "principal_type": "User",
        "principalId" if camel else "principal_id": "user-a",
    }
    token = _token_snapshot({"access_token": jwt(claims)}, client_id="client", epoch=0)
    assert token.user_id == "user-a"
    assert "user-a" not in repr(token)


@pytest.mark.parametrize(
    "override",
    [
        {"iss": "https://other.invalid"},
        {"client_id": "other"},
        {"principal_type": "Team"},
        {"principal_id": "different"},
        {"sub": ""},
        {"principal_type": None},
        {"sub": "bad\r\nheader", "principal_id": "bad\r\nheader"},
    ],
)
def test_access_token_identity_requires_matching_user_principal(override):
    claims = {
        "iss": "https://auth.x.ai",
        "client_id": "client",
        "sub": "user-a",
        "principal_type": "User",
        "principal_id": "user-a",
    }
    claims.update(override)
    token = _token_snapshot({"access_token": jwt(claims)}, client_id="client", epoch=0)
    assert token.user_id is None
