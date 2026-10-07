import asyncio
import json
import multiprocessing

import pytest

from grok_oauth.errors import CredentialPersistenceError, CredentialStoreInUse
from grok_oauth.models import TokenSnapshot
from grok_oauth.token_store import TokenStore


def sample(**changes):
    fields = {
        "slot": "default",
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "expires_at": 1234.5,
        "scope": "api:access",
        "client_id": "client",
        "version": 2,
        "epoch": 1,
    }
    fields.update(changes)
    return TokenSnapshot(**fields)


@pytest.mark.asyncio
async def test_private_atomic_round_trip_and_clear(tmp_path):
    path = tmp_path / "state" / "credentials.json"
    store = TokenStore(path)
    await store.open()
    await store.open()
    await store.commit(sample())
    assert await store.load() == sample()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o077 == 0
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    await store.clear()
    await store.clear()
    assert await store.load() is None
    await store.close()
    await store.close()


@pytest.mark.asyncio
async def test_corrupt_and_nonfinite_files_are_rejected_without_replacement(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('{"access_token":"secret"', encoding="utf-8")
    original = path.read_bytes()
    store = TokenStore(path)
    await store.open()
    with pytest.raises(CredentialPersistenceError):
        await store.load()
    assert path.read_bytes() == original
    path.write_text(
        json.dumps(
            {
                "slot": "default",
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": float("nan"),
                "scope": "s",
                "client_id": "c",
                "version": 1,
                "epoch": 0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CredentialPersistenceError):
        await store.load()
    await store.close()


def _try_lock(path, queue):
    async def run():
        store = TokenStore(path)
        try:
            await store.open()
        except CredentialStoreInUse:
            queue.put("in-use")
        else:
            queue.put("opened")
            await store.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_lifetime_lock_excludes_another_process(tmp_path):
    path = tmp_path / "credentials.json"
    store = TokenStore(path)
    await store.open()
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(target=_try_lock, args=(path, queue))
    process.start()
    process.join(5)
    assert process.exitcode == 0 and queue.get(timeout=1) == "in-use"
    await store.close()
    replacement = TokenStore(path)
    await replacement.open()
    await replacement.close()


def test_secret_repr_is_redacted():
    value = sample()
    assert value.access_token not in repr(value) and value.refresh_token not in repr(value)
