"""Windows filesystem operations for diagnostics and image delivery state."""

from __future__ import annotations

import json
import locale
import os
import uuid
from contextlib import suppress
from pathlib import Path

from .windows_acl import restrict_windows_handle
from .windows_files import locked_directory, open_binary

_LOG_NAME = "transport.jsonl"
_LOG_BACKUP = "transport.jsonl.1"


def _protect_stream(stream) -> None:
    import msvcrt

    restrict_windows_handle(msvcrt.get_osfhandle(stream.fileno()))


def append_windows_diagnostic(root: Path, data: bytes, *, max_bytes: int) -> None:
    """Append privately, rotating to one backup while the directory stays pinned."""
    if len(data) > max_bytes:
        raise OSError("diagnostic entry exceeds file limit")
    with (
        locked_directory(root.absolute()) as parent,
        parent.subdirectory("debug", create=True, acl_access=True) as directory,
    ):
        restrict_windows_handle(directory.handle, directory=True)
        with (
            suppress(FileNotFoundError),
            directory.open_binary(_LOG_BACKUP, acl_access=True) as backup,
        ):
            _protect_stream(backup)
        with directory.open_binary(_LOG_NAME, "ab", acl_access=True) as stream:
            _protect_stream(stream)
            if os.fstat(stream.fileno()).st_size + len(data) <= max_bytes:
                stream.write(data)
                return
        # Close both files before renaming. The pinned directory stays the target.
        directory.replace(_LOG_NAME, _LOG_BACKUP)
        with directory.open_binary(_LOG_NAME, "xb", acl_access=True) as stream:
            _protect_stream(stream)
            stream.write(data)


def read_windows_outbox(path: Path) -> dict:
    try:
        with open_binary(path.absolute()) as stream:
            data = stream.read()
    except FileNotFoundError:
        return {}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Older versions wrote with the process's locale encoding.
        text = data.decode(locale.getencoding())
    return json.loads(text)


def write_windows_outbox(path: Path, records: dict) -> None:
    """Sync UTF-8 data, then atomically replace state in the pinned directory."""
    path = path.absolute()
    data = json.dumps(records, ensure_ascii=False).encode("utf-8")
    temporary = f".outbox-{uuid.uuid4().hex}"
    created = False
    with locked_directory(path.parent, create=True) as directory:
        # Refuse pre-existing links or non-regular state files.
        with suppress(FileNotFoundError), directory.open_binary(path.name):
            pass
        try:
            with directory.open_binary(temporary, "xb", acl_access=True) as stream:
                created = True
                _protect_stream(stream)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            directory.replace(temporary, path.name)
            created = False
        finally:
            if created:
                with suppress(FileNotFoundError):
                    directory.unlink(temporary)
