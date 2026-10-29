import asyncio
import errno
import json
import multiprocessing
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from grok_oauth import token_store
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


def _assert_windows_acl(path):
    script = """
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:GROK_TEST_ACL_PATH
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
[pscustomobject]@{
    protected = $acl.AreAccessRulesProtected
    current = $current
    rules = @($rules | ForEach-Object {
        [pscustomobject]@{
            sid = $_.IdentityReference.Value
            inherited = $_.IsInherited
            allow = $_.AccessControlType -eq 'Allow'
            full_control = $_.FileSystemRights -eq 'FullControl'
        }
    })
} | ConvertTo-Json -Compress -Depth 4
"""
    completed = subprocess.run(
        [
            shutil.which("pwsh") or "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        env={
            **{key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"},
            "GROK_TEST_ACL_PATH": str(path),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    acl = json.loads(completed.stdout)
    assert acl["protected"]
    assert {rule["sid"] for rule in acl["rules"]} == {acl["current"], "S-1-5-18"}
    assert all(
        not rule["inherited"] and rule["allow"] and rule["full_control"] for rule in acl["rules"]
    )


@pytest.mark.asyncio
async def test_private_atomic_round_trip_and_clear(tmp_path):
    path = tmp_path / "state" / "credentials.json"
    # Simulate upgrading with an existing credential created before the Windows ACL fix.
    path.parent.mkdir()
    path.write_text(json.dumps(asdict(sample())), encoding="utf-8")
    store = TokenStore(path)
    await store.open()
    await store.open()
    assert await store.load() == sample()
    if os.name == "nt":
        _assert_windows_acl(path.parent)
        _assert_windows_acl(path)
    replacement = sample(version=3)
    await store.commit(replacement)
    if os.name == "nt":
        _assert_windows_acl(path)
    else:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o077 == 0
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    await store.close()
    await store.open()
    assert await store.load() == replacement
    await store.clear()
    await store.clear()
    assert await store.load() is None
    await store.close()
    await store.close()


@pytest.mark.asyncio
async def test_windows_commit_without_posix_file_operations(tmp_path, monkeypatch):
    path = tmp_path / "state" / "credentials.json"
    protected = []

    def restrict(path, *, directory=False):
        if not directory:
            # ACLs must be in place before any token bytes are written.
            assert Path(path).stat().st_size == 0
        protected.append(directory)

    original_open = os.open

    def open_file_only(path, *args, **kwargs):
        if Path(path).is_dir():
            raise PermissionError("directory descriptors unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(token_store, "_WINDOWS", True, raising=False)
    monkeypatch.setattr(token_store, "_restrict_windows_path", restrict, raising=False)
    monkeypatch.delattr(os, "fchmod", raising=False)
    monkeypatch.setattr(os, "open", open_file_only)
    store = TokenStore(path)
    await store.open()
    try:
        await store.commit(sample())
        assert await store.load() == sample()
        await store.clear()
        assert not path.exists()
        assert protected == [True, False]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stream_open", "windows_acl"])
async def test_failed_commit_closes_temporary_descriptor_and_preserves_existing_token(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / "state" / "credentials.json"
    store = TokenStore(path)
    await store.open()
    await store.commit(sample())
    failed_descriptors = []
    original_mkstemp = token_store.tempfile.mkstemp

    def record_mkstemp(*args, **kwargs):
        descriptor, name = original_mkstemp(*args, **kwargs)
        failed_descriptors.append(descriptor)
        return descriptor, name

    monkeypatch.setattr(token_store.tempfile, "mkstemp", record_mkstemp)
    if failure == "stream_open":

        def fail_fdopen(*args, **kwargs):
            raise OSError("simulated credential file open failure")

        monkeypatch.setattr(os, "fdopen", fail_fdopen)
    else:

        def fail_acl(path):
            assert path.stat().st_size == 0
            raise PermissionError("simulated ACL update failure")

        monkeypatch.setattr(token_store, "_WINDOWS", True)
        monkeypatch.setattr(token_store, "_restrict_windows_path", fail_acl)
    try:
        with pytest.raises(CredentialPersistenceError):
            await store.commit(sample(version=3))
        assert len(failed_descriptors) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(failed_descriptors[0])
        assert closed.value.errno == errno.EBADF
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))
        assert await store.load() == sample()
    finally:
        for descriptor in failed_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        await store.close()


@pytest.mark.asyncio
async def test_existing_credential_acl_failure_releases_store_lock(tmp_path, monkeypatch):
    path = tmp_path / "state" / "credentials.json"
    store = TokenStore(path)
    await store.open()
    await store.commit(sample())
    await store.close()

    def deny_existing_file(path, *, directory=False):
        if not directory:
            assert path.is_file()
            raise PermissionError("simulated existing credential ACL failure")

    with monkeypatch.context() as patch:
        patch.setattr(token_store, "_WINDOWS", True)
        patch.setattr(token_store, "_restrict_windows_path", deny_existing_file)
        with pytest.raises(CredentialPersistenceError):
            await store.open()

    # Do not close the failed instance before testing the second lock acquisition.
    replacement = TokenStore(path)
    await replacement.open()
    try:
        assert await replacement.load() == sample()
    finally:
        await replacement.close()


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
    context = multiprocessing.get_context("spawn")
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
