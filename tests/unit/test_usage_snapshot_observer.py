"""Protocol callbacks publish actual snapshots independently of host imports."""

import pytest
from test_responses import FakeHttp, response

from grok_oauth.errors import ProtocolError, StreamIncomplete
from grok_oauth.models import RequestPolicy
from grok_oauth.responses import ResponsesClient


@pytest.mark.parametrize(
    "terminal", ["response.completed", "response.failed", "response.incomplete"]
)
async def test_parsed_snapshots_are_published_before_terminal_delivery(terminal):
    seen = []
    intermediate = {"input_tokens": 8, "output_tokens": 0}
    final = {"input_tokens": 8, "output_tokens": 2}
    events = [
        {"type": "response.in_progress", "response": {"id": "resp-test", "usage": intermediate}},
        {"type": terminal, "response": response(usage=final)},
    ]
    client = ResponsesClient(
        FakeHttp(events=events), usage_observer=lambda raw, **kw: seen.append((raw, kw))
    )
    if terminal == "response.completed":
        stream = client.stream({"model": "grok"}, policy=RequestPolicy())
        result = await anext(stream)
        assert len(seen) == 2
        assert result.result.usage == final
        await stream.aclose()
    else:
        with pytest.raises(StreamIncomplete):
            [event async for event in client.stream({"model": "grok"}, policy=RequestPolicy())]
    assert seen == [
        (intermediate, {"response_id": "resp-test", "complete": False}),
        (final, {"response_id": "resp-test", "complete": True}),
    ]


async def test_missing_terminal_usage_does_not_upgrade_intermediate_snapshot():
    seen = []
    client = ResponsesClient(
        FakeHttp(
            events=[
                {
                    "type": "response.in_progress",
                    "response": {
                        "id": "resp-test",
                        "usage": {"input_tokens": 8, "output_tokens": 2},
                    },
                },
                {"type": "response.completed", "response": response()},
            ]
        ),
        usage_observer=lambda raw, **kw: seen.append((raw, kw)),
    )
    result = [event async for event in client.stream({"model": "grok"}, policy=RequestPolicy())]
    assert result[-1].result.usage["is_partial"] is True
    assert len(seen) == 1 and seen[0][1]["complete"] is False


async def test_observer_errors_and_mutation_do_not_change_chat_results():
    def observer(raw, **kwargs):
        raw["input_tokens"] = 999
        raise RuntimeError("observer failed")

    client = ResponsesClient(
        FakeHttp(result=response(usage={"input_tokens": 0, "output_tokens": 0})),
        usage_observer=observer,
    )
    result = await client.create({"model": "grok"}, policy=RequestPolicy())
    assert result.usage == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.parametrize("usage", [None, {}, {"total_tokens": 8}, {"input_tokens": True}])
async def test_no_valid_counter_means_no_observer_call(usage):
    seen = []
    client = ResponsesClient(
        FakeHttp(result=response(usage=usage)), usage_observer=lambda *a, **kw: seen.append((a, kw))
    )
    if usage == {"input_tokens": True}:
        with pytest.raises(ProtocolError):
            await client.create({"model": "grok"}, policy=RequestPolicy())
    else:
        await client.create({"model": "grok"}, policy=RequestPolicy())
    assert seen == []


async def test_snapshot_metadata_is_allowlisted():
    seen = []
    raw = response(usage={"input_tokens": 4, "prompt": "private"})
    raw["id"] = "private text\nsecret"
    client = ResponsesClient(
        FakeHttp(result=raw), usage_observer=lambda raw, **kw: seen.append((raw, kw))
    )
    await client.create({"model": "grok"}, policy=RequestPolicy())
    assert seen == [
        ({"input_tokens": 4, "is_partial": True}, {"response_id": None, "complete": True})
    ]
