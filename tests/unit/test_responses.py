import copy
import json
from contextlib import asynccontextmanager

import pytest

from grok_oauth.errors import EmptyOutput, ProtocolError, StreamIncomplete
from grok_oauth.models import RequestPolicy
from grok_oauth.responses import ResponsesClient, encode_messages, normalize_response


def response(output=None, status="completed", **kw):
    return dict(
        id="resp-test",
        model="grok-test",
        status=status,
        output=output
        if output is not None
        else [dict(type="message", content=[dict(type="output_text", text="ok")])],
        **kw,
    )


class FakeHttp:
    def __init__(self, result=None, events=()):
        self.result = result or response()
        self.events = events
        self.requests = []

    async def request_json(self, method, path, *, json, policy):
        self.requests.append((method, path, json, policy))
        return self.result

    @asynccontextmanager
    async def stream_sse(self, path, *, json, policy):
        self.requests.append(("POST", path, json, policy))

        async def stream():
            for event in self.events:
                yield ("data: " + __import__("json").dumps(event) + "\n\n").encode()

        yield stream()


def test_messages_preserve_roles_images_calls_and_encrypted_reasoning():
    history = [
        {"role": "developer", "content": "rules"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,TEST"}},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "think",
                    "encrypted": json.dumps(
                        {
                            "type": "grok_oauth_reasoning",
                            "items": [
                                {
                                    "type": "reasoning",
                                    "id": "rs",
                                    "encrypted_content": "sealed",
                                    "summary": [],
                                }
                            ],
                        }
                    ),
                }
            ],
            "tool_calls": [
                {
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "f", "arguments": '{"x":1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-a", "content": "done"},
    ]
    before = copy.deepcopy(history)
    encoded = encode_messages(history)
    assert history == before
    assert encoded[0]["role"] == "developer"
    assert encoded[1]["content"][1]["type"] == "input_image"
    assert encoded[2]["encrypted_content"] == "sealed"
    assert encoded[3] == {
        "type": "function_call",
        "call_id": "call-a",
        "name": "f",
        "arguments": '{"x":1}',
    }
    assert encoded[4] == {"type": "function_call_output", "call_id": "call-a", "output": "done"}


def test_result_preserves_parallel_calls_reasoning_usage_and_native_items():
    output = [
        {
            "type": "reasoning",
            "id": "r",
            "encrypted_content": "sealed",
            "summary": [{"type": "summary_text", "text": "thinking"}],
        },
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"a":2}'},
        {"type": "function_call", "call_id": "c2", "name": "g", "arguments": "{}"},
        {"type": "image_generation_call", "id": "img", "result": "sensitive-base64"},
    ]
    r = normalize_response(
        response(
            output,
            usage={
                "input_tokens": 11,
                "output_tokens": 3,
                "input_tokens_details": {"cached_tokens": 2},
            },
        )
    )
    assert [c["call_id"] for c in r.function_calls] == ["c1", "c2"]
    assert r.reasoning_items[0]["encrypted_content"] == "sealed"
    assert r.usage["input_tokens"] == 11
    assert len(r.native_items) == 1
    assert "sensitive-base64" not in repr(r)


@pytest.mark.parametrize("arguments", ["{bad", "[]", "null"])
def test_invalid_function_arguments_never_become_empty_object(arguments):
    with pytest.raises(ProtocolError):
        normalize_response(
            response(
                [{"type": "function_call", "call_id": "c", "name": "f", "arguments": arguments}]
            )
        )


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled", "in_progress"])
def test_nonterminal_or_failed_response_is_not_success(status):
    with pytest.raises((ProtocolError, StreamIncomplete)):
        normalize_response(response(status=status))


def test_empty_response_rejected_but_native_image_preserved():
    with pytest.raises(EmptyOutput):
        normalize_response(response([]))
    assert normalize_response(
        response([{"type": "image_generation_call", "id": "i", "result": "test"}])
    ).native_items
    assert normalize_response(response()).usage is None


async def test_client_forces_stateless_and_keeps_per_call_parameters():
    http = FakeHttp()
    payload = {"model": "custom", "input": [], "reasoning": {"effort": "low"}}
    r = await ResponsesClient(http).create(payload, policy=RequestPolicy())
    sent = http.requests[0][2]
    assert r.text == "ok" and sent["store"] is False
    assert sent["model"] == "custom" and sent["reasoning"] == {"effort": "low"}
    assert "store" not in payload


@pytest.mark.parametrize(
    "extra", [{"store": True}, {"previous_response_id": "r"}, {"conversation": "c"}]
)
async def test_server_state_options_are_rejected(extra):
    http = FakeHttp()
    with pytest.raises(ProtocolError):
        await ResponsesClient(http).create(
            {"input": [], "model": "m", **extra}, policy=RequestPolicy()
        )
    assert not http.requests


async def test_stream_deltas_once_then_aggregate():
    http = FakeHttp(
        events=[
            {"type": "response.output_text.delta", "delta": "o"},
            {"type": "response.output_text.delta", "delta": "k"},
            {"type": "response.completed", "response": response()},
        ]
    )
    events = [
        e
        async for e in ResponsesClient(http).stream(
            {"model": "m", "input": []}, policy=RequestPolicy()
        )
    ]
    assert [e.delta for e in events if e.kind == "text_delta"] == ["o", "k"]
    assert events[-1].kind == "completed" and events[-1].result.text == "ok"


async def test_interleaved_function_arguments_preserve_call_identity():
    final = response(
        [
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"a":1}'},
            {"type": "function_call", "call_id": "c2", "name": "g", "arguments": '{"b":2}'},
        ]
    )
    http = FakeHttp(
        events=[
            {"type": "response.function_call_arguments.delta", "item_id": "i1", "delta": '{"a":'},
            {"type": "response.function_call_arguments.delta", "item_id": "i2", "delta": '{"b":2}'},
            {"type": "response.function_call_arguments.delta", "item_id": "i1", "delta": "1}"},
            {"type": "response.completed", "response": final},
        ]
    )
    events = [
        e
        async for e in ResponsesClient(http).stream(
            {"model": "m", "input": []}, policy=RequestPolicy()
        )
    ]
    assert [e.item_id for e in events[:-1]] == ["i1", "i2", "i1"]
    assert events[-1].result.function_calls[0]["arguments"] == '{"a":1}'


async def test_stream_without_completed_is_partial_error():
    http = FakeHttp(events=[{"type": "response.output_text.delta", "delta": "half"}])
    with pytest.raises(StreamIncomplete) as error:
        [
            e
            async for e in ResponsesClient(http).stream(
                {"model": "m", "input": []}, policy=RequestPolicy()
            )
        ]
    assert error.value.partial is True


@pytest.mark.parametrize(
    "encrypted", ["opaque-anthropic-signature", '{"type":"other_provider","data":"sealed"}']
)
def test_foreign_reasoning_is_not_replayed_or_rejected(encrypted):
    encoded = encode_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "think", "encrypted": encrypted},
                    {"type": "text", "text": "previous answer"},
                ],
            }
        ]
    )
    assert encoded == [{"role": "assistant", "content": "previous answer"}]


@pytest.mark.parametrize(
    "state", ['{"type":"grok_oauth_reasoning","items":"wrong"}', '{"type":"grok_oauth_reasoning",']
)
def test_malformed_own_reasoning_state_rejected(state):
    with pytest.raises(ProtocolError):
        encode_messages([{"role": "assistant", "content": [{"type": "think", "encrypted": state}]}])


async def test_malformed_terminal_after_delta_preserves_partial():
    http = FakeHttp(
        events=[
            {"type": "response.output_text.delta", "delta": "half"},
            {"type": "response.completed", "response": response(status=[])},
        ]
    )
    with pytest.raises(ProtocolError) as error:
        [
            e
            async for e in ResponsesClient(http).stream(
                {"model": "m", "input": []}, policy=RequestPolicy()
            )
        ]
    assert error.value.partial


@pytest.mark.parametrize("summary", [[], None])
def test_null_reasoning_arrays_preserve_original_values(summary):
    r = normalize_response(
        response(
            [
                {"type": "reasoning", "summary": summary, "content": None},
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
            ]
        )
    )
    assert r.reasoning_items[0]["summary"] == summary and r.reasoning_items[0]["content"] is None


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"input_tokens": 0}, {"input_tokens": 0, "is_partial": True}),
        ({"output_tokens": 5}, {"output_tokens": 5, "is_partial": True}),
        (
            {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
            {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
        ),
        (
            {"input_tokens": 8, "output_tokens": 3, "total_tokens": 99},
            {"input_tokens": 8, "output_tokens": 3, "is_partial": True},
        ),
        ({"input_tokens": 8, "total_tokens": 11}, {"input_tokens": 8, "is_partial": True}),
        (
            {"input_tokens_details": {"cached_tokens": 4, "secret": "body"}},
            {"input_tokens_details": {"cached_tokens": 4}, "is_partial": True},
        ),
        (
            {"input_tokens": 2, "input_tokens_details": {"cached_tokens": 9}},
            {"input_tokens": 2, "is_partial": True},
        ),
        ({"input_tokens": True, "output_tokens": -1, "secret": "body"}, None),
        ({"total_tokens": 8}, None),
        ([], None),
        (None, None),
    ],
)
def test_safe_token_usage_preserves_only_explicit_valid_counters(usage, expected):
    from grok_oauth.token_usage import safe_token_usage

    original = copy.deepcopy(usage)
    actual = safe_token_usage(usage)
    assert actual == expected
    assert usage == original
    if actual and "input_tokens_details" in actual:
        actual["input_tokens_details"]["cached_tokens"] = 123
        assert usage == original


def test_success_usage_excludes_unknown_metadata():
    raw = response(
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 900,
            "body": "secret",
            "input_tokens_details": {"cached_tokens": 0, "token": "secret"},
        }
    )
    assert normalize_response(raw).usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "is_partial": True,
    }


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled", "in_progress"])
def test_rejected_response_keeps_safe_usage(status):
    with pytest.raises((ProtocolError, StreamIncomplete)) as caught:
        normalize_response(response(status=status, usage={"input_tokens": 8, "body": "secret"}))
    assert caught.value.token_usage == {"input_tokens": 8, "is_partial": True}


@pytest.mark.parametrize("bad_output", [[], "invalid", [None]])
def test_invalid_output_keeps_safe_usage(bad_output):
    with pytest.raises((ProtocolError, EmptyOutput)) as caught:
        normalize_response(response(output=bad_output, usage={"output_tokens": 0}))
    assert caught.value.token_usage == {"output_tokens": 0, "is_partial": True}


def test_invalid_cached_count_keeps_other_reported_counters():
    with pytest.raises(ProtocolError) as caught:
        normalize_response(
            response(
                usage={
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "input_tokens_details": {"cached_tokens": 9},
                }
            )
        )
    assert caught.value.token_usage == {"input_tokens": 2, "output_tokens": 3, "is_partial": True}


@pytest.mark.parametrize("kind", ["response.failed", "response.incomplete", "error"])
@pytest.mark.parametrize("nested", [False, True])
async def test_stream_failure_keeps_only_received_safe_usage(kind, nested):
    usage = {"input_tokens": 9, "input_tokens_details": {"cached_tokens": 2}, "prompt": "secret"}
    event = {"type": kind, **({"response": {"usage": usage}} if nested else {"usage": usage})}
    client = ResponsesClient(
        FakeHttp(events=[{"type": "response.output_text.delta", "delta": "half"}, event])
    )
    with pytest.raises(StreamIncomplete) as caught:
        [item async for item in client.stream({"model": "m", "input": []}, policy=RequestPolicy())]
    assert caught.value.token_usage == {
        "input_tokens": 9,
        "input_tokens_details": {"cached_tokens": 2},
        "is_partial": True,
    }
    assert caught.value.partial


async def test_completed_stream_close_failure_keeps_completed_usage():
    class ClosingHttp(FakeHttp):
        @asynccontextmanager
        async def stream_sse(self, path, *, json, policy):
            async def stream():
                yield (
                    "data: "
                    + __import__("json").dumps(
                        {
                            "type": "response.completed",
                            "response": response(usage={"input_tokens": 8, "output_tokens": 2}),
                        }
                    )
                    + "\n\n"
                ).encode()

            yield stream()
            raise ProtocolError("close failed")

    client = ResponsesClient(ClosingHttp())
    received = []
    with pytest.raises(ProtocolError) as caught:
        async for event in client.stream({"model": "m", "input": []}, policy=RequestPolicy()):
            received.append(event)
    assert len(received) == 1 and received[0].kind == "completed"
    assert caught.value.token_usage == {"input_tokens": 8, "output_tokens": 2}


async def test_stream_failure_without_usage_does_not_invent_counters():
    client = ResponsesClient(FakeHttp(events=[{"type": "response.failed"}]))
    with pytest.raises(StreamIncomplete) as caught:
        [item async for item in client.stream({"model": "m", "input": []}, policy=RequestPolicy())]
    assert caught.value.token_usage is None


@pytest.mark.parametrize("marker", [False, True, "partial"])
def test_raw_completeness_marker_is_not_trusted(marker):
    from grok_oauth.token_usage import safe_token_usage

    assert safe_token_usage({"input_tokens": 2, "output_tokens": 3, "is_partial": marker}) == {
        "input_tokens": 2,
        "output_tokens": 3,
    }


async def test_stream_repeated_usage_snapshots_are_not_added():
    events = [
        {"type": "response.in_progress", "response": {"usage": {"input_tokens": 8}}},
        {"type": "response.in_progress", "response": {"usage": {"input_tokens": 8}}},
        {
            "type": "response.failed",
            "response": {"usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 99}},
        },
    ]
    client = ResponsesClient(FakeHttp(events=events))
    with pytest.raises(StreamIncomplete) as caught:
        [item async for item in client.stream({"model": "m", "input": []}, policy=RequestPolicy())]
    assert caught.value.token_usage == {"input_tokens": 10, "output_tokens": 1, "is_partial": True}


async def test_stream_cancellation_keeps_previously_received_usage():
    import asyncio

    entered = asyncio.Event()

    class CancellableHttp(FakeHttp):
        @asynccontextmanager
        async def stream_sse(self, path, *, json, policy):
            async def stream():
                event = {"type": "response.in_progress", "response": {"usage": {"input_tokens": 0}}}
                yield ("data: " + __import__("json").dumps(event) + "\n\n").encode()
                entered.set()
                await asyncio.Event().wait()

            yield stream()

    client = ResponsesClient(CancellableHttp())

    async def consume():
        return [
            item
            async for item in client.stream({"model": "m", "input": []}, policy=RequestPolicy())
        ]

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.token_usage == {"input_tokens": 0, "is_partial": True}


async def test_stream_missing_terminal_retains_last_reported_snapshot():
    client = ResponsesClient(
        FakeHttp(
            events=[{"type": "response.in_progress", "response": {"usage": {"output_tokens": 3}}}]
        )
    )
    with pytest.raises(StreamIncomplete) as caught:
        [item async for item in client.stream({"model": "m", "input": []}, policy=RequestPolicy())]
    assert caught.value.token_usage == {"output_tokens": 3, "is_partial": True}


@pytest.mark.parametrize(
    "snapshot",
    [
        {"input_tokens": 0},
        {"output_tokens": 4},
        {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
    ],
)
async def test_completed_without_usage_keeps_received_snapshot_as_partial(snapshot):
    events = [
        {"type": "response.in_progress", "response": {"usage": snapshot}},
        {"type": "response.completed", "response": response()},
    ]
    actual = [
        item
        async for item in ResponsesClient(FakeHttp(events=events)).stream(
            {"model": "m", "input": []}, policy=RequestPolicy()
        )
    ]
    assert len(actual) == 1 and actual[0].kind == "completed"
    assert actual[0].result.usage == {**snapshot, "is_partial": True}
    assert "is_partial" not in snapshot


async def test_completed_usage_takes_precedence_over_previous_snapshot():
    events = [
        {"type": "response.in_progress", "response": {"usage": {"input_tokens": 8}}},
        {
            "type": "response.completed",
            "response": response(usage={"input_tokens": 9, "output_tokens": 2}),
        },
    ]
    actual = [
        item
        async for item in ResponsesClient(FakeHttp(events=events)).stream(
            {"model": "m", "input": []}, policy=RequestPolicy()
        )
    ]
    assert actual[0].result.usage == {"input_tokens": 9, "output_tokens": 2}


async def test_completed_fallback_close_failure_retains_partial_snapshot():
    class ClosingHttp(FakeHttp):
        @asynccontextmanager
        async def stream_sse(self, path, *, json, policy):
            async def stream():
                for event in [
                    {
                        "type": "response.in_progress",
                        "response": {
                            "usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}
                        },
                    },
                    {"type": "response.completed", "response": response()},
                ]:
                    yield ("data: " + __import__("json").dumps(event) + "\n\n").encode()

            yield stream()
            raise ProtocolError("close failed")

    received = []
    with pytest.raises(ProtocolError) as caught:
        async for event in ResponsesClient(ClosingHttp()).stream(
            {"model": "m", "input": []}, policy=RequestPolicy()
        ):
            received.append(event)
    expected = {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11, "is_partial": True}
    assert len(received) == 1 and received[0].result.usage == expected
    assert caught.value.token_usage == expected
