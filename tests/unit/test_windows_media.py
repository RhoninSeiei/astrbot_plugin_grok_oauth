import asyncio
import ctypes
import json
import os
import struct
import subprocess
import threading
from pathlib import PureWindowsPath

import pytest
from test_media import image_bytes
from test_video_import import movie

from grok_oauth import video_store, windows_files
from grok_oauth.errors import AssetNotFound, ImageTooLarge, UnsafeMediaSource
from grok_oauth.media import AssetStore
from grok_oauth.models import AssetScope
from grok_oauth.video_store import VideoStore

_SCOPE = AssetScope("qq", "group:1", "conversation")


class _WindowsOS:
    name = "nt"

    def __getattr__(self, name):
        if name == "O_DIRECTORY":
            raise AttributeError(name)
        return getattr(os, name)


@pytest.mark.asyncio
async def test_video_metadata_round_trip_without_directory_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(video_store, "os", _WindowsOS())
    root = tmp_path / "metadata"
    store = VideoStore(root)
    try:
        store._write({"assets": {}, "jobs": {}})
    finally:
        await store.close()
    assert json.loads((root / "metadata.json").read_text(encoding="utf-8")) == {
        "assets": {},
        "jobs": {},
    }
    reopened = VideoStore(root)
    try:
        assert await reopened.list_jobs(scope=_SCOPE) == []
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "reference", [r"C:\Incoming Files\图片.png", "file:///C:/Incoming%20Files/图片.png"]
)
def test_windows_media_path_syntax(reference):
    assert windows_files.parse_local_reference(reference) == PureWindowsPath(
        r"C:\Incoming Files\图片.png"
    )


@pytest.mark.parametrize(
    "reference",
    [
        r"C:incoming\file.png",
        r"C:\incoming\..\file.png",
        r"C:\incoming\file.png:stream",
        r"C:\incoming\NUL.png",
        r"C:\incoming\file.png.",
        r"\\server\share\file.png",
        r"\\?\C:\incoming\file.png",
        "file://server/share/file.png",
        "file:///C:/incoming/file.png?query=1",
        "file:///C:/incoming/file.png#fragment",
        "file:///C:/incoming/%00file.png",
        "file:///C:/incoming/%2e%2e/file.png",
    ],
)
def test_windows_media_rejects_unsafe_path_syntax(reference):
    with pytest.raises(OSError):
        windows_files.parse_local_reference(reference)


@pytest.mark.asyncio
@pytest.mark.parametrize("file_url", [False, True])
async def test_local_image_string_import_and_limit(tmp_path, file_url):
    incoming = tmp_path / "incoming 中文"
    incoming.mkdir()
    image = incoming / "图片.png"
    content = image_bytes()
    image.write_bytes(content)
    source = image.as_uri() if file_url else str(image)
    assets = AssetStore(tmp_path / "assets", max_image_bytes=len(content))
    try:
        asset_id = await assets.import_reference(source, scope=_SCOPE, allowed_roots=(incoming,))
        assert (await assets.resolve(asset_id, scope=_SCOPE)).read_bytes() == content
        image.write_bytes(content + b"x")
        with pytest.raises(ImageTooLarge):
            await assets.import_reference(source, scope=_SCOPE, allowed_roots=(incoming,))
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_local_video_import_limit(tmp_path):
    root = tmp_path / "incoming"
    root.mkdir()
    path = root / "video.mp4"
    content = movie()
    path.write_bytes(content + b"x")
    store = VideoStore(tmp_path / "assets", max_video_bytes=len(content))
    try:
        with pytest.raises(UnsafeMediaSource):
            await store.import_reference(
                path.as_uri(), scope=_SCOPE, binding="account", allowed_roots=(root,)
            )
        assert not store._metadata.exists()
    finally:
        await store.close()


if os.name == "nt":

    def test_windows_file_handle_excludes_mutation_and_closes(tmp_path):
        source = tmp_path / "source"
        source.write_bytes(b"payload")
        renamed = tmp_path / "renamed"
        with windows_files.open_binary(source) as stream:
            with pytest.raises(PermissionError):
                source.rename(renamed)
            with pytest.raises(PermissionError):
                source.write_bytes(b"replacement")
            assert stream.read() == b"payload"
        source.rename(renamed)
        assert renamed.read_bytes() == b"payload"

    @pytest.mark.asyncio
    async def test_windows_video_asset_rejects_replacement_hard_link(tmp_path):
        store = VideoStore(tmp_path / "assets")
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(movie())
        try:
            asset = await store.store_bytes(movie(), scope=_SCOPE)
            stored = store._files / f"{asset.asset_id}.mp4"
            stored.unlink()
            os.link(outside, stored)
            with pytest.raises(AssetNotFound):
                await store.read_bytes(asset.asset_id, scope=_SCOPE)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_cancelled_windows_image_import_releases_native_handles(tmp_path, monkeypatch):
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        source = incoming / "image.png"
        source.write_bytes(image_bytes())
        entered = threading.Event()
        release = threading.Event()
        original_open = windows_files._open_handle

        def hold_file_open(path, **kwargs):
            handle = original_open(path, **kwargs)
            if path == PureWindowsPath(source) and not kwargs["directory"]:
                entered.set()
                if not release.wait(5):
                    windows_files._kernel32().CloseHandle(handle)
                    raise AssertionError("Test did not release the native file")
            return handle

        monkeypatch.setattr(windows_files, "_open_handle", hold_file_open)
        store = AssetStore(tmp_path / "assets")
        task = asyncio.create_task(
            store.import_reference(source, scope=_SCOPE, allowed_roots=(incoming,))
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(PermissionError):
                source.unlink()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            source.unlink()
            assert json.loads(store._metadata_path.read_text(encoding="utf-8"))["assets"] == {}
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await store.close()


def test_windows_relative_directory_handles_use_data_access_without_delete_sharing(monkeypatch):
    calls = []

    class Kernel32:
        def GetDriveTypeW(self, root):
            return 3  # DRIVE_FIXED

        def CreateFileW(self, path, access, share, security, disposition, flags, template):
            calls.append((None, path, access, share))
            return len(calls)

        def GetFileType(self, handle):
            return 1  # FILE_TYPE_DISK

        def GetFileInformationByHandle(self, handle, info):
            info._obj.attributes = 0x10  # FILE_ATTRIBUTE_DIRECTORY
            return True

        def CloseHandle(self, handle):
            return True

    class NtDll:
        def NtCreateFile(
            self,
            handle,
            access,
            attributes,
            ios,
            allocation,
            flags,
            share,
            disposition,
            options,
            extra,
            extra_length,
        ):
            attributes = attributes._obj
            name = attributes.object_name.contents
            child = ctypes.string_at(name.buffer, name.length).decode("utf-16-le")
            assert options & 0x00200000  # FILE_OPEN_REPARSE_POINT
            calls.append((attributes.root_directory, child, access, share))
            handle._obj.value = len(calls)
            return 0

    monkeypatch.setattr(windows_files, "_kernel32", Kernel32)
    monkeypatch.setattr(windows_files, "_ntdll", NtDll)
    with windows_files.locked_directory(PureWindowsPath(r"C:\parent\child")):
        assert [(parent, name) for parent, name, *_ in calls] == [
            (None, "\\\\?\\C:\\"),
            (1, "parent"),
            (2, "child"),
        ]
        for _, _, access, share in calls:
            assert access & 0x01  # FILE_LIST_DIRECTORY participates in share checks.
            assert share == 0x03  # FILE_SHARE_READ | FILE_SHARE_WRITE, without delete.


if os.name == "nt":

    def test_windows_reparse_path_redirect_cannot_escape_held_directory(tmp_path, monkeypatch):
        incoming = tmp_path / "incoming"
        outside = tmp_path / "outside"
        incoming.mkdir()
        outside.mkdir()
        (incoming / "payload").write_bytes(b"inside")
        (outside / "payload").write_bytes(b"outside")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", "redirect", "outside"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        redirect = tmp_path / "redirect"
        assert redirect.is_junction()
        assert (redirect / "payload").read_bytes() == b"outside"
        with windows_files.locked_directory(incoming) as directory:
            # Inject a pathname lookup that now resolves through the junction.
            # The real child open and mutations must use the held object instead.
            monkeypatch.setattr(directory, "path", PureWindowsPath(redirect))
            with directory.open_binary("payload") as stream:
                assert stream.read() == b"inside"
            with directory.subdirectory("created", create=True) as child:
                with child.open_binary("input", "xb") as stream:
                    stream.write(b"new")
                with child.open_binary("output", "xb") as stream:
                    stream.write(b"old")
                child.replace("input", "output")
                with child.open_binary("output") as stream:
                    assert stream.read() == b"new"
                child.unlink("output")
                with pytest.raises(FileNotFoundError):
                    child.unlink("output")
            assert (incoming / "created").is_dir()
            assert not (outside / "created").exists()
            assert (outside / "payload").read_bytes() == b"outside"

    def test_windows_in_place_reparse_point_cannot_redirect_held_directory(tmp_path, capsys):
        from ctypes import wintypes

        incoming = tmp_path / "incoming"
        outside = tmp_path / "outside"
        incoming.mkdir()
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside-only")
        protected_output = outside / "published"
        protected_output.write_bytes(b"untouched output")
        api = windows_files._kernel32()
        control = api.DeviceIoControl
        control.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        control.restype = wintypes.BOOL
        mount_point = 0xA0000003
        substitute = ("\\??\\" + str(outside)).encode("utf-16-le")
        display = str(outside).encode("utf-16-le")
        names = substitute + bytes(2) + display + bytes(2)
        data = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(display)) + names
        set_buffer = struct.pack("<IHH", mount_point, len(data), 0) + data
        clear_buffer = struct.pack("<IHH", mount_point, 0, 0)

        def run_control(handle, code, payload):
            buffer = ctypes.create_string_buffer(payload)
            returned = wintypes.DWORD()
            if control(handle, code, buffer, len(payload), None, 0, ctypes.byref(returned), None):
                return None
            return ctypes.get_last_error()

        denied = {5, 32, 1314}  # Access denied, sharing violation, privilege not held.
        installed = False
        created = False
        try:
            with windows_files.locked_directory(incoming) as directory:
                mutation_handle = api.CreateFileW(
                    "\\\\?\\" + str(incoming),
                    0x40000000,  # GENERIC_WRITE includes FILE_WRITE_ATTRIBUTES.
                    0x07,  # Share read, write and delete.
                    None,
                    3,  # OPEN_EXISTING
                    0x02200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                    None,
                )
                if mutation_handle == ctypes.c_void_p(-1).value:
                    error = ctypes.get_last_error()
                    assert error in denied, f"Unexpected WinError {error} opening mutation handle"
                    mutation_handle = None
                else:
                    error = None
                try:
                    if mutation_handle is not None:
                        error = run_control(mutation_handle, 0x000900A4, set_buffer)
                        installed = error is None
                        assert installed or error in denied, (
                            f"Unexpected WinError {error} setting reparse point"
                        )
                    with capsys.disabled():
                        result = "installed" if installed else f"denied (WinError {error})"
                        print(f"Native in-place junction while parent handle is held: {result}")
                    if installed:
                        assert incoming.is_junction()
                        # Show that an absolute lookup would already reach the other object.
                        assert (incoming / "sentinel").read_bytes() == b"outside-only"
                    else:
                        assert not incoming.is_junction()
                    with pytest.raises(OSError):
                        with directory.open_binary("sentinel"):
                            pytest.fail("Relative open reached a file outside the held directory")
                    try:
                        with directory.subdirectory("created", create=True):
                            created = True
                    except OSError:
                        # Some filesystems refuse new children on a reparse object.
                        assert installed
                    try:
                        with directory.open_binary("scratch", "xb") as stream:
                            stream.write(b"inside-only")
                    except OSError:
                        assert installed
                    try:
                        directory.replace("scratch", "published")
                    except OSError:
                        assert installed
                    try:
                        directory.unlink("published")
                    except OSError:
                        assert installed
                    assert sentinel.read_bytes() == b"outside-only"
                    assert protected_output.read_bytes() == b"untouched output"
                    assert not (outside / "created").exists()
                    assert not (outside / "scratch").exists()
                    if installed:
                        with capsys.disabled():
                            print(
                                "In-place junction: relative read and mutations preserved outside files"
                            )
                finally:
                    if mutation_handle is not None:
                        try:
                            if installed:
                                error = run_control(mutation_handle, 0x000900AC, clear_buffer)
                                installed = error is not None
                                assert not installed, (
                                    f"WinError {error} removing test reparse point"
                                )
                        finally:
                            api.CloseHandle(mutation_handle)
        finally:
            if incoming.is_junction():
                # Remove only the junction itself if the native restore failed.
                incoming.rmdir()
            else:
                (incoming / "scratch").unlink(missing_ok=True)
                (incoming / "published").unlink(missing_ok=True)
        assert not incoming.is_junction()
        assert (incoming / "created").is_dir() == created


def test_windows_native_write_handle_requests_file_attributes(monkeypatch):
    class NtDll:
        def NtCreateFile(self, handle, access, *args):
            assert access & 0x40000000  # GENERIC_WRITE
            assert access & 0x00100000  # SYNCHRONIZE
            assert access & 0x00000080  # FILE_READ_ATTRIBUTES
            handle._obj.value = 2
            return 0

    monkeypatch.setattr(windows_files, "_ntdll", NtDll)
    assert (
        windows_files._open_child(
            "output", parent=1, access=0x40000000, share=1, disposition=1, directory=False
        )
        == 2
    )
