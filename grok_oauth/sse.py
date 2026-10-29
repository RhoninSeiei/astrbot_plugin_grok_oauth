"""Bounded SSE framing with byte-safe UTF-8 and explicit incomplete input."""

import json
import re
from collections.abc import AsyncIterator

from .errors import ProtocolError, StreamIncomplete

_LINE_END = re.compile(rb"[\r\n]")


async def iter_json_sse_events(
    chunks: AsyncIterator[bytes], *, max_event_bytes: int = 2 * 1024 * 1024
) -> AsyncIterator[dict]:
    """Decode complete JSON events while limiting buffered event bytes.

    Args:
        chunks: Raw HTTP body chunks.
        max_event_bytes: Maximum encoded bytes per event, including comments.

    Yields:
        JSON object events. The legacy DONE marker is not a Responses terminal.

    Raises:
        ProtocolError: Invalid UTF-8, JSON, shape or size.
        StreamIncomplete: The connection ends inside an event.
    """
    if type(max_event_bytes) is not int or max_event_bytes < 1:
        raise ValueError("max_event_bytes must be positive")
    pending = bytearray()
    data: list[bytes] = []
    event_size = 0

    first_line = True

    def consume(line: bytes):
        nonlocal event_size, first_line
        event_size += len(line) + 1
        if event_size > max_event_bytes:
            raise ProtocolError("SSE event exceeds byte limit")
        if first_line:
            first_line = False
            line = line.removeprefix(b"\xef\xbb\xbf")
        if not line:
            event_size = 0
            if not data:
                return None
            payload = b"\n".join(data)
            data.clear()
            if payload == b"[DONE]":
                return None
            try:
                obj = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise ProtocolError("Invalid SSE JSON event") from None
            if not isinstance(obj, dict):
                raise ProtocolError("SSE event must be an object")
            return obj
        if line.startswith(b"data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(b" ") else value)
        elif line == b"data":
            data.append(b"")
        return None

    def append_part(chunk, start, end):
        if event_size + len(pending) + end - start > max_event_bytes:
            raise ProtocolError("SSE event exceeds byte limit")
        pending.extend(memoryview(chunk)[start:end])

    # Scan each network chunk once, even when a large JSON line spans chunks.
    after_cr = False
    async for chunk in chunks:
        start = 0
        for match in _LINE_END.finditer(chunk):
            end = match.start()
            if not (after_cr and start == end and chunk[end] == 10):
                append_part(chunk, start, end)
                obj = consume(bytes(pending))
                pending.clear()
                if obj is not None:
                    yield obj
            after_cr = chunk[end] == 13
            start = end + 1
        if start < len(chunk):
            after_cr = False
            append_part(chunk, start, len(chunk))
    if pending or data:
        raise StreamIncomplete("Connection ended within an SSE event")
