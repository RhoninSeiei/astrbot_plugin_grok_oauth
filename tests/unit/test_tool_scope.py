import copy
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_grok_oauth.astrbot_adapter.tool_scope import (
    consume_tool_call,
    consume_usage_call,
    issue_tool_call,
    issue_usage_call,
)

USAGE_TOOL_NAMES = ("grok_usage_status", "grok_usage_breakdown")


class Provider:
    def __init__(self):
        self._closed = False
        self._runtime = SimpleNamespace(
            closed=False,
            owner_id="test-owner",
            oauth=SimpleNamespace(epoch=2, binding_generation=3),
        )


def test_permit_is_json_invisible_and_single_use_across_copies():
    provider = Provider()
    args = {"query": "facts", "allowed_domains": ["example.com"]}
    original = json.dumps(args, sort_keys=True)
    issue_tool_call(provider, "grok_web_search", args)
    assert isinstance(args["query"], str) and json.dumps(args, sort_keys=True) == original
    assert consume_tool_call(provider._runtime, "grok_web_search", json.loads(original)) is None
    copied = copy.deepcopy(args)
    assert consume_tool_call(provider._runtime, "grok_web_search", copied) is provider
    assert consume_tool_call(provider._runtime, "grok_web_search", args) is None


@pytest.mark.parametrize(
    "change", ["args", "tool", "runtime", "closed_provider", "closed_runtime", "expired"]
)
def test_invalid_permits_fail_closed(change, monkeypatch):
    provider = Provider()
    args = {"prompt": "image", "n": 1}
    issue_tool_call(provider, "grok_image_generate", args)
    runtime = provider._runtime
    name = "grok_image_generate"
    if change == "args":
        args["n"] = 2
    elif change == "tool":
        name = "grok_image_edit"
    elif change == "runtime":
        runtime = SimpleNamespace(closed=False, owner_id="other")
    elif change == "closed_provider":
        provider._closed = True
    elif change == "closed_runtime":
        runtime.closed = True
    else:
        import astrbot_plugin_grok_oauth.astrbot_adapter.tool_scope as scope

        monkeypatch.setattr(scope.time, "monotonic", lambda: 10**20)
    assert consume_tool_call(runtime, name, args) is None


def test_identical_calls_have_independent_permissions():
    provider = Provider()
    first = {"query": "same"}
    second = {"query": "same"}
    issue_tool_call(provider, "grok_web_search", first)
    issue_tool_call(provider, "grok_web_search", second)
    assert consume_tool_call(provider._runtime, "grok_web_search", first) is provider
    assert consume_tool_call(provider._runtime, "grok_web_search", second) is provider
    assert consume_tool_call(provider._runtime, "grok_web_search", second) is None


def test_unowned_tool_or_missing_query_is_not_issued():
    provider = Provider()
    args = {"query": "facts"}
    issue_tool_call(provider, "codex_web_search", args)
    assert type(args["query"]) is str
    assert consume_tool_call(provider._runtime, "codex_web_search", args) is None


def test_proof_does_not_keep_provider_alive():
    import weakref

    provider = Provider()
    runtime = provider._runtime
    args = {"query": "facts"}
    issue_tool_call(provider, "grok_web_search", args)
    ref = weakref.ref(provider)
    del provider
    assert ref() is None
    assert consume_tool_call(runtime, "grok_web_search", args) is None


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
def test_empty_usage_proof_is_name_bound_single_use_and_json_invisible(tool_name):
    provider = Provider()
    issued = issue_usage_call(provider, tool_name)

    assert issued == tool_name
    assert json.loads(json.dumps(issued)) == tool_name
    assert consume_usage_call(provider._runtime, json.loads(json.dumps(issued))) is None
    copied = copy.deepcopy(issued)
    assert consume_usage_call(provider._runtime, issued) is provider
    assert consume_usage_call(provider._runtime, copied) is None


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
def test_empty_usage_proof_cannot_be_disguised_as_other_usage_tool(tool_name):
    provider = Provider()
    issued = issue_usage_call(provider, tool_name)
    other_name = next(name for name in USAGE_TOOL_NAMES if name != tool_name)
    disguised = type(issued)(other_name, issued._proof)

    assert consume_usage_call(provider._runtime, disguised) is None
    assert consume_usage_call(provider._runtime, issued) is provider
