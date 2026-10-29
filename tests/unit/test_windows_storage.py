import json
import os
import shutil
import stat
import subprocess
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from grok_oauth import windows_storage


@pytest.fixture(autouse=True)
def windows_backend(monkeypatch):
    if os.name == "nt":
        return

    # Exercise storage logic on Linux. Windows CI uses the actual handles and ACLs.
    class Directory:
        def __init__(self, handle):
            self.handle = handle
            self.path = Path(f"/proc/self/fd/{handle}")

        @contextmanager
        def subdirectory(self, name, *, create=False, acl_access=False):
            path = self.path / name
            if create:
                with suppress(FileExistsError):
                    path.mkdir()
            with open_directory(path) as directory:
                yield directory

        @contextmanager
        def open_binary(self, name, mode="rb", *, acl_access=False):
            path = self.path / name
            try:
                existing = path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                    raise OSError("Unsafe test file")
            with path.open(mode) as stream:
                yield stream

        def replace(self, source, destination):
            os.replace(source, destination, src_dir_fd=self.handle, dst_dir_fd=self.handle)

        def unlink(self, name):
            os.unlink(name, dir_fd=self.handle)

    @contextmanager
    def open_directory(path):
        handle = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield Directory(handle)
        finally:
            os.close(handle)

    @contextmanager
    def locked_directory(path, *, create=False, acl_access=False):
        if create:
            path.mkdir(parents=True, exist_ok=True)
        for ancestor in (path, *path.parents):
            if not stat.S_ISDIR(ancestor.lstat().st_mode):
                raise OSError("Unsafe test directory")
        with open_directory(path) as directory:
            yield directory

    @contextmanager
    def open_binary(path):
        with locked_directory(path.parent) as directory, directory.open_binary(path.name) as stream:
            yield stream

    monkeypatch.setattr(windows_storage, "locked_directory", locked_directory)
    monkeypatch.setattr(windows_storage, "open_binary", open_binary)
    monkeypatch.setattr(windows_storage, "restrict_windows_handle", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_storage, "_protect_stream", lambda stream: None)


def _powershell(script, **environment):
    completed = subprocess.run(
        [
            shutil.which("pwsh") or "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'\n" + script,
        ],
        env={
            **{name: value for name, value in os.environ.items() if name.upper() != "PSMODULEPATH"},
            **environment,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _acl(path):
    result = _powershell(
        """
$acl = Get-Acl -LiteralPath $env:GROK_TEST_ACL_PATH
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
[pscustomobject]@{
    sddl = $acl.Sddl
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
""",
        GROK_TEST_ACL_PATH=str(path),
    )
    return json.loads(result)


def _assert_private_acl(path):
    if os.name != "nt":
        return
    acl = _acl(path)
    assert acl["protected"]
    assert {rule["sid"] for rule in acl["rules"]} == {acl["current"], "S-1-5-18"}
    assert all(
        not rule["inherited"] and rule["allow"] and rule["full_control"] for rule in acl["rules"]
    )


def test_windows_diagnostics_append_and_rotate_one_private_backup(tmp_path):
    records = [f'{{"n":{n}}}\n'.encode() for n in range(5)]
    for record in records:
        windows_storage.append_windows_diagnostic(tmp_path, record, max_bytes=16)
    folder = tmp_path / "debug"
    current = folder / "transport.jsonl"
    backup = folder / "transport.jsonl.1"
    assert sorted(item.name for item in folder.iterdir()) == [current.name, backup.name]
    assert current.read_bytes() == records[4]
    assert backup.read_bytes() == records[2] + records[3]
    for path in (folder, current, backup):
        _assert_private_acl(path)


@pytest.mark.parametrize("name", ["transport.jsonl", "transport.jsonl.1", "unrelated"])
def test_windows_diagnostics_keep_external_hard_link_content_and_acl(tmp_path, name):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    folder = tmp_path / "debug"
    folder.mkdir()
    os.link(outside, folder / name)
    before = _acl(outside)["sddl"] if os.name == "nt" else None
    if name == "unrelated":
        # The directory ACL must not propagate to arbitrary existing child files.
        windows_storage.append_windows_diagnostic(tmp_path, b"{}\n", max_bytes=128)
    else:
        with pytest.raises(OSError):
            windows_storage.append_windows_diagnostic(tmp_path, b"{}\n", max_bytes=128)
        other = "transport.jsonl.1" if name == "transport.jsonl" else "transport.jsonl"
        assert not (folder / other).exists()
    assert outside.read_bytes() == b"keep"
    if os.name == "nt":
        assert isinstance(before, str) and before
        assert _acl(outside)["sddl"] == before


def test_windows_diagnostics_refuse_reparse_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = tmp_path / "debug"
    if os.name == "nt":
        _powershell(
            "New-Item -ItemType Junction -Path $env:GROK_TEST_LINK -Target $env:GROK_TEST_TARGET | Out-Null",
            GROK_TEST_LINK=str(folder),
            GROK_TEST_TARGET=str(outside),
        )
    else:
        folder.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        windows_storage.append_windows_diagnostic(tmp_path, b"{}\n", max_bytes=128)
    assert list(outside.iterdir()) == []


def test_windows_outbox_reopens_legacy_and_utf8_state(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox.json"
    assert windows_storage.read_windows_outbox(outbox) == {}
    previous = {"job": {"status": "unknown", "message": "旧记录"}}
    monkeypatch.setattr(windows_storage.locale, "getencoding", lambda: "cp936")
    outbox.write_bytes(json.dumps(previous, ensure_ascii=False).encode("cp936"))
    assert windows_storage.read_windows_outbox(outbox) == previous
    updated = {"job": {"status": "sent", "message": "发送完成"}}
    windows_storage.write_windows_outbox(outbox, updated)
    assert windows_storage.read_windows_outbox(outbox) == updated
    assert "发送完成" in outbox.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".outbox-*")) == []
    _assert_private_acl(outbox)


def test_windows_outbox_keeps_previous_state_when_sync_fails(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox.json"
    previous = {"job": {"status": "generated"}}
    windows_storage.write_windows_outbox(outbox, previous)

    def fail_sync(descriptor):
        raise OSError("test sync failure")

    monkeypatch.setattr(windows_storage.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="test sync failure"):
        windows_storage.write_windows_outbox(outbox, {"job": {"status": "sent"}})
    assert windows_storage.read_windows_outbox(outbox) == previous
    assert list(tmp_path.glob(".outbox-*")) == []


def test_windows_outbox_creates_and_reopens_nested_state_directory(tmp_path):
    outbox = tmp_path / "新目录" / "nested" / "outbox.json"
    assert windows_storage.read_windows_outbox(outbox) == {}
    records = {"job": {"status": "sent", "message": "发送完成"}}
    windows_storage.write_windows_outbox(outbox, records)
    assert windows_storage.read_windows_outbox(outbox) == records


class _RedirectedParent:
    """Change what a path names while the operation's native directory is open."""

    def __init__(self, root):
        self.physical = root / "physical"
        self.other = root / "other"
        self.physical.mkdir()
        self.other.mkdir()
        self.path = self.physical
        self.drive = None

    def __enter__(self):
        if os.name == "nt":
            occupied = {drive.upper() for drive in os.listdrives()}
            self.drive = next(
                f"{letter}:" for letter in "ZYXWVUTSRQP" if f"{letter}:\\" not in occupied
            )
            self._subst(str(self.physical))
            self.path = Path(self.drive + "\\")
        return self

    def _subst(self, target):
        completed = subprocess.run(
            ["subst.exe", self.drive, target],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    def redirect(self):
        if self.drive is not None:
            self._subst("/D")
            self._subst(str(self.other))
        else:
            retained = self.physical.with_name("retained")
            self.physical.rename(retained)
            self.path.symlink_to(self.other, target_is_directory=True)
            self.physical = retained

    def __exit__(self, *args):
        if self.drive is not None:
            self._subst("/D")


def test_windows_log_rotation_stays_in_original_directory_after_parent_path_changes(
    tmp_path, monkeypatch
):
    with _RedirectedParent(tmp_path) as parent:
        windows_storage.append_windows_diagnostic(parent.path, b'{"n":1}\n', max_bytes=8)
        unrelated = parent.other / "debug"
        unrelated.mkdir()
        unrelated_current = unrelated / "transport.jsonl"
        unrelated_backup = unrelated / "transport.jsonl.1"
        unrelated_current.write_bytes(b"outside current")
        unrelated_backup.write_bytes(b"outside backup")
        real_fstat = os.fstat

        def redirect_before_rotation(descriptor):
            result = real_fstat(descriptor)
            parent.redirect()
            return result

        with monkeypatch.context() as patched:
            patched.setattr(windows_storage.os, "fstat", redirect_before_rotation)
            windows_storage.append_windows_diagnostic(parent.path, b'{"n":2}\n', max_bytes=8)
        assert (parent.physical / "debug" / "transport.jsonl").read_bytes() == b'{"n":2}\n'
        assert (parent.physical / "debug" / "transport.jsonl.1").read_bytes() == b'{"n":1}\n'
        assert unrelated_current.read_bytes() == b"outside current"
        assert unrelated_backup.read_bytes() == b"outside backup"


@pytest.mark.parametrize("fail", [False, True])
def test_windows_outbox_replace_and_cleanup_stay_in_original_directory_after_parent_path_changes(
    tmp_path, monkeypatch, fail
):
    with _RedirectedParent(tmp_path) as parent:
        old = {"job": {"status": "generated"}}
        new = {"job": {"status": "sent"}}
        windows_storage.write_windows_outbox(parent.path / "outbox.json", old)
        other_outbox = parent.other / "outbox.json"
        other_outbox.write_bytes(b"outside state")
        real_fsync = os.fsync
        other_temporary = None

        def redirect_before_replace_or_cleanup(descriptor):
            nonlocal other_temporary
            real_fsync(descriptor)
            (staged,) = parent.physical.glob(".outbox-*")
            other_temporary = parent.other / staged.name
            other_temporary.write_bytes(b"outside temporary")
            parent.redirect()
            if fail:
                raise OSError("test sync failure")

        with monkeypatch.context() as patched:
            patched.setattr(windows_storage.os, "fsync", redirect_before_replace_or_cleanup)
            if fail:
                with pytest.raises(OSError, match="test sync failure"):
                    windows_storage.write_windows_outbox(parent.path / "outbox.json", new)
            else:
                windows_storage.write_windows_outbox(parent.path / "outbox.json", new)
        assert windows_storage.read_windows_outbox(parent.physical / "outbox.json") == (
            old if fail else new
        )
        assert list(parent.physical.glob(".outbox-*")) == []
        assert other_outbox.read_bytes() == b"outside state"
        assert other_temporary.read_bytes() == b"outside temporary"
