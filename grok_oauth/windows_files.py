"""Handle-based access to regular files on local Windows volumes."""

from __future__ import annotations

import ctypes
import errno
import os
import re
from contextlib import ExitStack, contextmanager, suppress
from ctypes import wintypes
from functools import cache
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_DELETE = 0x00010000
_WRITE_DAC = 0x00040000
_MAXIMUM_ALLOWED = 0x02000000
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x0001
_FILE_SHARE_WRITE = 0x0002
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_NT_CREATE_DISPOSITION = {_OPEN_EXISTING: 1, _CREATE_NEW: 2, _OPEN_ALWAYS: 3}
_OBJ_CASE_INSENSITIVE = 0x0040
_FILE_DIRECTORY_FILE = 0x0001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x0020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_TYPE_DISK = 1
_FILE_RENAME_INFORMATION = 10
_FILE_DISPOSITION_INFORMATION = 13
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_LOCAL_DRIVE_TYPES = {2, 3, 5, 6}  # Removable, fixed, CD-ROM, RAM disk.
_DRIVE = re.compile(r"[A-Za-z]:")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])", re.I)


class _FileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", wintypes.FILETIME),
        ("accessed", wintypes.FILETIME),
        ("written", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.USHORT),
        ("maximum_length", wintypes.USHORT),
        ("buffer", ctypes.c_void_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.ULONG),
        ("root_directory", wintypes.HANDLE),
        ("object_name", ctypes.POINTER(_UnicodeString)),
        ("attributes", wintypes.ULONG),
        ("security_descriptor", ctypes.c_void_p),
        ("security_quality_of_service", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    # The first field is a union of NTSTATUS and PVOID. Only its size is needed here.
    _fields_ = [("status_or_pointer", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _FileRenameInformation(ctypes.Structure):
    _fields_ = [
        ("replace_if_exists", ctypes.c_ubyte),
        ("root_directory", wintypes.HANDLE),
        ("name_length", wintypes.ULONG),
        ("name", wintypes.WCHAR * 1),
    ]


def validate_windows_path(path: str | Path | PureWindowsPath) -> PureWindowsPath:
    """Validate path syntax before any filesystem or network access."""
    value = PureWindowsPath(path)
    if not value.is_absolute() or not _DRIVE.fullmatch(value.drive):
        raise OSError(errno.EINVAL, "A local absolute Windows path is required")
    for part in value.parts[1:]:
        if (
            part.endswith((".", " "))
            or any(char in '<>:"|?*' or ord(char) < 32 for char in part)
            or _RESERVED.fullmatch(part.partition(".")[0].rstrip(" "))
        ):
            raise OSError(errno.EINVAL, "Invalid Windows path component")
    return value


def parse_local_reference(reference: str, *, allow_localhost: bool = False) -> PureWindowsPath:
    """Accept drive paths and file:///C:/... without treating C: as a scheme."""
    if reference[:5].lower() == "file:":
        try:
            parsed = urlsplit(reference)
        except ValueError:
            raise OSError(errno.EINVAL, "Invalid local file URL") from None
        hosts = {"", "localhost"} if allow_localhost else {""}
        if parsed.netloc.lower() not in hosts or parsed.query or parsed.fragment:
            raise OSError(errno.EINVAL, "Invalid local file URL")
        reference = unquote(parsed.path)
        if reference.startswith("/"):
            reference = reference[1:]
    return validate_windows_path(reference)


@cache
def _kernel32():
    api = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    api.GetDriveTypeW.restype = wintypes.UINT
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileType.argtypes = [wintypes.HANDLE]
    api.GetFileType.restype = wintypes.DWORD
    api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    api.GetFileInformationByHandle.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


@cache
def _ntdll():
    api = ctypes.WinDLL("ntdll.dll")
    api.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    api.NtCreateFile.restype = wintypes.LONG
    api.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    api.NtSetInformationFile.restype = wintypes.LONG
    api.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    api.RtlNtStatusToDosError.restype = wintypes.ULONG
    return api


def _open_child(name, *, parent, access, share, disposition, directory):
    encoded = name.encode("utf-16-le", errors="surrogatepass")
    if len(encoded) > 0xFFFD:
        raise OSError(errno.ENAMETOOLONG, "Windows path component is too long")
    buffer = ctypes.create_string_buffer(encoded, len(encoded) + 2)
    name_string = _UnicodeString(len(encoded), ctypes.sizeof(buffer), ctypes.addressof(buffer))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent,
        ctypes.pointer(name_string),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    options = _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
    if directory and disposition == _OPEN_ALWAYS:
        options |= _FILE_DIRECTORY_FILE
    handle = wintypes.HANDLE()
    api = _ntdll()
    status = api.NtCreateFile(
        ctypes.byref(handle),
        # Generic write does not include the read-attributes access needed to
        # verify the file type and links on the returned handle.
        access | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        ctypes.byref(attributes),
        ctypes.byref(_IoStatusBlock()),
        None,
        0,  # Use default file attributes on creation.
        share,
        _NT_CREATE_DISPOSITION[disposition],
        options,
        None,
        0,
    )
    if status < 0:
        raise ctypes.WinError(api.RtlNtStatusToDosError(status))
    return handle.value


def _open_handle(path, *, access, disposition, directory, parent=None):
    api = _kernel32()
    share = _FILE_SHARE_READ
    if directory:
        # Child creation and replacement need write sharing on the directory.
        share |= _FILE_SHARE_WRITE
    if parent is None:
        if not directory or path != PureWindowsPath(path.anchor):
            raise ValueError("Only a Windows drive root may be opened without a parent handle")
        # Only the drive anchor is opened by its absolute name.
        handle = api.CreateFileW(
            "\\\\?\\" + str(path),
            access,
            share,
            None,
            disposition,
            _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        # Resolve one name relative to a validated directory handle. Reopening the
        # absolute name could follow a reparse point set on an already open parent.
        handle = _open_child(
            path.name,
            parent=parent,
            access=access,
            share=share,
            disposition=disposition,
            directory=directory,
        )
    try:
        info = _FileInfo()
        if api.GetFileType(handle) != _FILE_TYPE_DISK:
            raise OSError(errno.EINVAL, "A Windows disk file is required")
        if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or bool(info.attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory
            or (not directory and info.links != 1)
        ):
            raise OSError(errno.EPERM, "Unsafe Windows filesystem object")
    except BaseException:
        api.CloseHandle(handle)
        raise
    return handle


def _set_file_information(handle, buffer, information_class):
    api = _ntdll()
    status = api.NtSetInformationFile(
        handle,
        ctypes.byref(_IoStatusBlock()),
        ctypes.byref(buffer),
        ctypes.sizeof(buffer),
        information_class,
    )
    if status < 0:
        raise ctypes.WinError(api.RtlNtStatusToDosError(status))


@contextmanager
def _directory(path, *, parent=None, create=False, acl_access=False):
    # An attributes-only open does not participate in share checks.
    access = _MAXIMUM_ALLOWED if acl_access else _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES
    try:
        handle = _open_handle(
            path, access=access, disposition=_OPEN_EXISTING, directory=True, parent=parent
        )
    except FileNotFoundError:
        if not create or parent is None:
            raise
        handle = _open_handle(
            path, access=access, disposition=_OPEN_ALWAYS, directory=True, parent=parent
        )
    directory = _LockedDirectory(path, handle)
    try:
        yield directory
    finally:
        directory.handle = None
        _kernel32().CloseHandle(handle)


@contextmanager
def locked_directory(
    path: Path | PureWindowsPath, *, create: bool = False, acl_access: bool = False
):
    """Keep the verified directory and its parents open without delete sharing."""
    value = validate_windows_path(path)
    api = _kernel32()
    if api.GetDriveTypeW(value.anchor) not in _LOCAL_DRIVE_TYPES:
        raise OSError(errno.EINVAL, "A local Windows volume is required")
    with ExitStack() as handles:
        root_only = len(value.parts) == 1
        directory = handles.enter_context(
            _directory(PureWindowsPath(value.anchor), acl_access=acl_access and root_only)
        )
        for index, name in enumerate(value.parts[1:], 1):
            directory = handles.enter_context(
                directory.subdirectory(
                    name, create=create, acl_access=acl_access and index == len(value.parts) - 1
                )
            )
        yield directory


class _LockedDirectory:
    def __init__(self, path, handle):
        self.path = path
        self.handle = handle

    def _child_path(self, name):
        if self.handle is None:
            raise OSError(errno.EBADF, "Windows directory handle is closed")
        if not name or PureWindowsPath(name).name != name:
            raise ValueError("A single Windows name is required")
        return validate_windows_path(self.path / name)

    def subdirectory(self, name: str, *, create: bool = False, acl_access: bool = False):
        return _directory(
            self._child_path(name), parent=self.handle, create=create, acl_access=acl_access
        )

    def replace(self, source: str, destination: str) -> None:
        """Replace a child using names relative to this validated directory handle."""
        source_path = self._child_path(source)
        destination_path = self._child_path(destination)
        handle = _open_handle(
            source_path,
            access=_DELETE | _FILE_READ_ATTRIBUTES,
            disposition=_OPEN_EXISTING,
            directory=False,
            parent=self.handle,
        )
        api = _kernel32()
        try:
            with suppress(FileNotFoundError):
                target = _open_handle(
                    destination_path,
                    access=_FILE_READ_ATTRIBUTES,
                    disposition=_OPEN_EXISTING,
                    directory=False,
                    parent=self.handle,
                )
                api.CloseHandle(target)
            encoded = destination.encode("utf-16-le", errors="surrogatepass")
            offset = _FileRenameInformation.name.offset
            size = ctypes.sizeof(_FileRenameInformation) + len(encoded) + 2
            buffer = ctypes.create_string_buffer(size)
            info = _FileRenameInformation.from_buffer(buffer)
            info.replace_if_exists = 1
            # A simple name with a null RootDirectory renames within the source
            # handle's own parent. A non-null handle can cause the kernel to reopen
            # that directory by its full path.
            info.root_directory = None
            info.name_length = len(encoded)
            ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
            _set_file_information(handle, buffer, _FILE_RENAME_INFORMATION)
        finally:
            api.CloseHandle(handle)

    def unlink(self, name: str) -> None:
        """Delete an opened regular child and close its delete-pending handle."""
        handle = _open_handle(
            self._child_path(name),
            access=_DELETE | _FILE_READ_ATTRIBUTES,
            disposition=_OPEN_EXISTING,
            directory=False,
            parent=self.handle,
        )
        try:
            _set_file_information(handle, ctypes.c_ubyte(1), _FILE_DISPOSITION_INFORMATION)
        finally:
            _kernel32().CloseHandle(handle)

    @contextmanager
    def open_binary(self, name: str, mode: str = "rb", *, acl_access: bool = False):
        """Open a single verified child without opening the parent chain again."""
        import msvcrt

        path = self._child_path(name)
        if mode not in {"rb", "ab", "xb"}:
            raise ValueError("A Windows binary mode is required")
        disposition = {"rb": _OPEN_EXISTING, "ab": _OPEN_ALWAYS, "xb": _CREATE_NEW}[mode]
        access = _GENERIC_READ if mode == "rb" else _GENERIC_WRITE
        if acl_access:
            access |= _WRITE_DAC
        flags = os.O_BINARY | os.O_NOINHERIT | (os.O_RDONLY if mode == "rb" else os.O_WRONLY)
        if mode == "ab":
            flags |= os.O_APPEND
        handle = _open_handle(
            path, access=access, disposition=disposition, directory=False, parent=self.handle
        )
        try:
            descriptor = msvcrt.open_osfhandle(handle, flags)
        except BaseException:
            _kernel32().CloseHandle(handle)
            raise
        try:
            stream = os.fdopen(descriptor, "wb" if mode == "xb" else mode)
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            yield stream


@contextmanager
def open_binary(path: Path | PureWindowsPath, mode: str = "rb", *, acl_access: bool = False):
    """Open a regular file while holding its validated parent handles."""
    value = validate_windows_path(path)
    with (
        locked_directory(value.parent) as directory,
        directory.open_binary(value.name, mode, acl_access=acl_access) as stream,
    ):
        yield stream


def read_local_file(path: Path | PureWindowsPath, allowed_roots, limit: int) -> bytes:
    """Read at most one byte past the limit beneath an explicit trusted directory."""
    value = validate_windows_path(path)
    for configured in allowed_roots:
        root = validate_windows_path(configured)
        if root == PureWindowsPath(root.anchor):
            raise OSError(errno.EPERM, "A drive root cannot allow local imports")
        if value != root and value.is_relative_to(root):
            # Reuse the configured spelling even on a case-sensitive NTFS directory.
            candidate = root / value.relative_to(root)
            with open_binary(candidate) as stream:
                return stream.read(limit + 1)
    raise OSError(errno.EPERM, "Local file is outside allowed roots")
