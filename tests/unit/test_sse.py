import pytest

from grok_oauth.errors import ProtocolError, StreamIncomplete
from grok_oauth.sse import iter_json_sse_events


async def chunks(wire, size=1):
    for offset in range(0, len(wire), size):
        yield wire[offset : offset + size]


async def test_utf8_crlf_multiline_and_comment():
    wire = (
        ": heartbeat\r\n\r\nevent: response.output_text.delta\r\n"
        'data: {"type":"response.output_text.delta",\r\n'
        'data: "delta":"图像"}\r\n\r\n'
        'data: {"type":"response.completed","response":{}}\n\n'
    ).encode()
    events = [e async for e in iter_json_sse_events(chunks(wire), max_event_bytes=4096)]
    assert events[0]["delta"] == "图像"
    assert events[1]["type"] == "response.completed"


@pytest.mark.parametrize("wire", [b"data: nope\n\n", b"data: []\n\n", b'data: {"x":"\xff"}\n\n'])
async def test_malformed_event_is_typed_and_does_not_quote_data(wire):
    with pytest.raises(ProtocolError) as error:
        [e async for e in iter_json_sse_events(chunks(wire), max_event_bytes=4096)]
    assert "nope" not in str(error.value)


async def test_size_limit_applies_before_complete_event():
    with pytest.raises(ProtocolError):
        [e async for e in iter_json_sse_events(chunks(b"data: " + b"x" * 100), max_event_bytes=32)]


async def test_unterminated_event_is_incomplete():
    with pytest.raises(StreamIncomplete):
        [e async for e in iter_json_sse_events(chunks(b'data: {"a":1}'), max_event_bytes=100)]


async def test_done_marker_is_not_a_completed_response():
    events = [
        e async for e in iter_json_sse_events(chunks(b"data: [DONE]\n\n"), max_event_bytes=100)
    ]
    assert events == []


async def test_bare_carriage_return_is_a_line_separator():
    events = [
        e async for e in iter_json_sse_events(chunks(b'data: {"a":1}\r\r', 3), max_event_bytes=100)
    ]
    assert events == [{"a": 1}]
