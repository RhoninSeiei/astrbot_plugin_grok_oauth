import copy
import json

import pytest
from astrbot.core.agent.message import Message, TextPart, ThinkPart, ToolCall
from astrbot.core.provider.entities import ToolCallsResult
from astrbot_plugin_grok_oauth.astrbot_adapter.compat import to_llm_response
from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError
from astrbot_plugin_grok_oauth.grok_oauth.responses import encode_messages, normalize_response


def raw_response():
    return {
        "id": "response-synthetic",
        "model": "grok-4.7",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "r1", "encrypted_content": "opaque-a"},
            {
                "type": "web_search_call",
                "id": "w1",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"url": "https://docs.python.org/3/", "title": "Python"}],
                },
                "encrypted_content": "opaque-search",
            },
            {
                "type": "reasoning",
                "id": "r2",
                "summary": None,
                "content": None,
                "encrypted_content": "opaque-b",
            },
            {
                "type": "message",
                "role": "assistant",
                "id": "m1",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Answer", "annotations": []}],
            },
            {
                "type": "function_call",
                "id": "fc1",
                "call_id": "call1",
                "name": "f",
                "arguments": '{"x": 1}',
                "status": "completed",
            },
        ],
    }


def host_history(response):
    assistant = Message(
        role="assistant",
        content=[
            TextPart(text=response.completion_text),
            ThinkPart(
                think=response.reasoning_content or "", encrypted=response.reasoning_signature
            ),
        ],
        tool_calls=[
            ToolCall(id="call1", function=ToolCall.FunctionBody(name="f", arguments='{"x":1}'))
        ],
    )
    return ToolCallsResult(
        tool_calls_info=assistant,
        tool_calls_result=[Message(role="tool", tool_call_id="call1", content="tool-result")],
    ).to_openai_messages()


def test_real_host_continuation_preserves_order_and_opaque_outputs_without_duplicates():
    raw = raw_response()
    before = copy.deepcopy(raw)
    response = to_llm_response(normalize_response(raw), allow_web_search=True)
    history = host_history(response)
    encoded = encode_messages(history)
    assert encoded[:-1] == raw["output"]
    assert encoded[-1] == {
        "type": "function_call_output",
        "call_id": "call1",
        "output": "tool-result",
    }
    assert raw == before
    assert "opaque-search" not in repr(normalize_response(raw))
    assert "opaque-search" not in response.completion_text


@pytest.mark.parametrize("edit", ["text", "arguments"])
def test_host_edits_are_preserved_and_discard_stale_continuation(edit):
    response = to_llm_response(normalize_response(raw_response()), allow_web_search=True)
    history = host_history(response)
    if edit == "text":
        history[0]["content"][0]["text"] = "Edited answer"
    else:
        history[0]["tool_calls"][0]["function"]["arguments"] = '{"x":2}'
    encoded = encode_messages(history)
    assert not any(item.get("type") in {"reasoning", "web_search_call"} for item in encoded)
    if edit == "text":
        assert encoded[0]["content"] == "Edited answer"
    else:
        assert (
            next(x for x in encoded if x.get("type") == "function_call")["arguments"] == '{"x":2}'
        )


@pytest.mark.parametrize(
    "change",
    [
        "unknown_type",
        "wrong_role",
        "duplicate_call",
        "bad_digest",
        "version",
        "mixed",
        "inconsistent_items",
    ],
)
def test_malformed_own_continuation_fails_closed(change):
    response = to_llm_response(normalize_response(raw_response()), allow_web_search=True)
    history = host_history(response)
    state = json.loads(history[0]["content"][1]["encrypted"])
    if change == "unknown_type":
        state["output"][0]["type"] = "code_interpreter_call"
    elif change == "wrong_role":
        state["output"][3]["role"] = "system"
    elif change == "duplicate_call":
        state["output"].append(state["output"][-1])
    elif change == "bad_digest":
        state["display_digest"] = []
    elif change == "mixed":
        history[0]["content"].append(
            {
                "type": "think",
                "encrypted": json.dumps({"type": "grok_oauth_reasoning", "items": []}),
            }
        )
    elif change == "inconsistent_items":
        state["items"] = []
    else:
        state["version"] = 999
    history[0]["content"][1]["encrypted"] = json.dumps(state)
    with pytest.raises(ProtocolError):
        encode_messages(history)


def test_valid_state_cannot_hide_malformed_own_envelope():
    response = to_llm_response(normalize_response(raw_response()), allow_web_search=True)
    history = host_history(response)
    history[0]["content"].append(
        {"type": "think", "encrypted": '{"type":"grok_oauth_reasoning","version":2,'}
    )
    with pytest.raises(ProtocolError):
        encode_messages(history)
