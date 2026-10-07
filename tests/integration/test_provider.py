import asyncio
import copy
import json
import time
import weakref
from types import SimpleNamespace

import pytest
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.register import provider_cls_map, provider_registry
from astrbot_plugin_grok_oauth.astrbot_adapter.provider import GrokOAuthProvider
from astrbot_plugin_grok_oauth.astrbot_adapter.registration import (
    register_provider,
    unregister_provider,
)
from astrbot_plugin_grok_oauth.astrbot_adapter.registry_state import bind_runtime, clear_runtime
from astrbot_plugin_grok_oauth.grok_oauth.catalog import ModelCatalog
from astrbot_plugin_grok_oauth.grok_oauth.errors import (
    PermissionDenied,
    ProtocolError,
    RegistrationConflict,
    ServiceClosed,
    StreamIncomplete,
    UnsupportedModelParameter,
)
from astrbot_plugin_grok_oauth.grok_oauth.responses import ResponseEvent, normalize_response

TYPE = "grok_oauth_chat_completion"


class Responses:
    def __init__(self):
        self.calls = []
        self.raw = dict(
            id="r1",
            model="grok-4.6",
            status="completed",
            output=[{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        )

    async def create(self, payload, *, policy):
        self.calls.append((payload, policy))
        return normalize_response(self.raw)

    async def stream(self, payload, *, policy):
        self.calls.append((payload, policy))
        yield ResponseEvent("text_delta", delta="o")
        yield ResponseEvent("text_delta", delta="k")
        yield ResponseEvent("completed", result=normalize_response(self.raw))


@pytest.fixture
def bundle():
    runtime = SimpleNamespace(
        closed=False,
        responses=Responses(),
        catalog=ModelCatalog(),
        config={},
        owner_id="test-runtime",
        providers=weakref.WeakSet(),
    )
    bind_runtime(runtime)
    handle = register_provider(runtime.owner_id, GrokOAuthProvider)
    yield runtime
    unregister_provider(handle)
    clear_runtime(runtime)


def provider(bundle, **kwargs):
    return GrokOAuthProvider({"type": TYPE, "id": "grok-test", "model": "grok-4.6", **kwargs}, {})


def test_owned_registration_template_and_collision():
    before = dict(provider_cls_map)
    handle = register_provider("a", GrokOAuthProvider)
    try:
        meta = provider_cls_map[TYPE]
        assert meta.provider_display_name == "Grok Oauth"
        from astrbot_plugin_grok_oauth.astrbot_adapter.compat import provider_source_templates

        template = provider_source_templates()["Grok Oauth"]
        assert template["type"] == TYPE
        assert template["provider_type"] == "chat_completion"
        assert register_provider("a", GrokOAuthProvider).owner_id == "a"
        with pytest.raises(RegistrationConflict):
            register_provider("b", GrokOAuthProvider)
        assert len([m for m in provider_registry if m.type == TYPE]) == 1
    finally:
        unregister_provider(handle)
    assert provider_cls_map == before


async def test_provider_construct_and_catalog_have_no_network(bundle):
    p = provider(bundle)
    assert isinstance(p, GrokOAuthProvider)
    assert "grok-4.6" in await p.get_models()
    assert p.get_current_key() == "oauth-managed"
    assert p.capabilities["chat"]["observed"] == "unknown"
    assert bundle.responses.calls == []
    await p.terminate()
    assert bundle.closed is False


async def test_text_messages_parameters_usage_and_reasoning(bundle):
    p = provider(bundle, reasoning_effort="low")
    bundle.responses.raw["usage"] = {
        "input_tokens": 10,
        "output_tokens": 3,
        "input_tokens_details": {"cached_tokens": 4},
    }
    bundle.responses.raw["output"].insert(
        0,
        {
            "type": "reasoning",
            "id": "rs",
            "summary": [{"type": "summary_text", "text": "thought"}],
            "encrypted_content": "sealed",
        },
    )
    result = await p.text_chat(
        prompt="hi",
        contexts=[{"role": "user", "content": "before"}],
        system_prompt="system",
        reasoning_effort="high",
    )
    assert isinstance(result, LLMResponse)
    assert result.completion_text == "ok" and result.usage.input_other == 6
    assert json.loads(result.reasoning_signature)["items"][0]["encrypted_content"] == "sealed"
    assert result.raw_completion is None
    sent = bundle.responses.calls[0][0]
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["input"][0]["role"] == "system"
    assert len(sent["input"]) == 3


async def test_real_toolset_keeps_schema_and_returns_structured_calls(bundle):
    parameters = {
        "type": "object",
        "properties": {"x": {"type": "integer", "minimum": 1}},
        "required": ["x"],
        "additionalProperties": False,
    }
    tools = ToolSet([FunctionTool(name="f", description="test", parameters=parameters)])
    bundle.responses.raw["output"] = [
        {"type": "function_call", "call_id": "call1", "name": "f", "arguments": '{"x":2}'}
    ]
    original = copy.deepcopy(parameters)
    result = await provider(bundle).text_chat(prompt="call", func_tool=tools)
    assert result.tools_call_args == [{"x": 2}]
    assert result.tools_call_ids == ["call1"] and result.role == "tool"
    assert bundle.responses.calls[0][0]["tools"][0]["parameters"] == original
    assert parameters == original


async def test_stream_has_two_deltas_and_one_nonchunk_aggregate(bundle):
    chunks = [c async for c in provider(bundle).text_chat_stream(prompt="hi")]
    assert [(c.completion_text, c.is_chunk) for c in chunks] == [
        ("o", True),
        ("k", True),
        ("ok", False),
    ]


async def test_provider_shutdown_rejects_use_but_keeps_shared_runtime(bundle):
    first, second = provider(bundle), provider(bundle, id="other")
    await first.terminate()
    with pytest.raises(ServiceClosed):
        await first.text_chat(prompt="no")
    assert (await second.text_chat(prompt="yes")).completion_text == "ok"
    bundle.closed = True
    with pytest.raises(ServiceClosed):
        await second.text_chat(prompt="no")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reasoning_effort": "max"},
        {"model": "grok-4.5", "reasoning_effort": "xhigh"},
        {"native_tools": ["web_search"]},
        {"audio_urls": ["test.wav"]},
    ],
)
async def test_unsupported_capability_fails_before_network(bundle, kwargs):
    with pytest.raises(UnsupportedModelParameter):
        await provider(bundle).text_chat(prompt="test", **kwargs)
    assert bundle.responses.calls == []


async def test_provider_cancels_only_its_inflight_tasks(bundle):
    entered = asyncio.Event()

    async def slow(payload, *, policy):
        entered.set()
        await asyncio.Event().wait()

    bundle.responses.create = slow
    p = provider(bundle)
    task = asyncio.create_task(p.text_chat(prompt="wait"))
    await entered.wait()
    await p.terminate()
    assert task.cancelled()
    assert not bundle.closed


async def test_stream_deadline_survives_separate_anext_tasks(bundle):
    async def blocked(payload, *, policy):
        yield ResponseEvent("text_delta", delta="first")
        await asyncio.Event().wait()

    bundle.responses.stream = blocked
    p = provider(bundle)
    stream = p.text_chat_stream(prompt="test", timeout=0.05)
    assert (await asyncio.create_task(anext(stream))).completion_text == "first"
    started = time.monotonic()
    with pytest.raises(StreamIncomplete) as error:
        await asyncio.wait_for(asyncio.create_task(anext(stream)), timeout=0.5)
    assert error.value.partial
    assert time.monotonic() - started < 0.3
    await stream.aclose()
    assert not p._tasks


async def test_stream_terminate_cancels_actual_producer_across_anext_tasks(bundle):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def blocked(payload, *, policy):
        try:
            yield ResponseEvent("text_delta", delta="first")
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    bundle.responses.stream = blocked
    p = provider(bundle)
    stream = p.text_chat_stream(prompt="test")
    await asyncio.create_task(anext(stream))
    next_task = asyncio.create_task(anext(stream))
    await entered.wait()
    await p.terminate()
    assert cleaned.is_set()
    with pytest.raises((asyncio.CancelledError, ServiceClosed)):
        await next_task
    await stream.aclose()


async def test_early_consumer_close_closes_inner_http_generator(bundle):
    cleaned = asyncio.Event()

    async def infinite(payload, *, policy):
        try:
            while True:
                yield ResponseEvent("text_delta", delta="x")
        finally:
            cleaned.set()

    bundle.responses.stream = infinite
    stream = provider(bundle).text_chat_stream(prompt="hi")
    await anext(stream)
    await stream.aclose()
    assert cleaned.is_set()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"response_format": {"type": "json_object"}},
        {"tools": [{"type": "web_search"}]},
        {"max_tokens": 12},
    ],
)
async def test_unknown_parameters_fail_before_request(bundle, kwargs):
    with pytest.raises(UnsupportedModelParameter):
        await provider(bundle).text_chat(prompt="hi", **kwargs)
    assert not bundle.responses.calls


async def test_full_queue_after_http_completion_cannot_expire_success(bundle):
    completed = asyncio.Event()

    async def fast(payload, *, policy):
        for index in range(8):
            yield ResponseEvent("text_delta", delta=str(index))
        yield ResponseEvent("completed", result=normalize_response(bundle.responses.raw))
        completed.set()

    bundle.responses.stream = fast
    stream = provider(bundle).text_chat_stream(prompt="test", timeout=0.1)
    first = await asyncio.create_task(anext(stream))
    await completed.wait()
    await asyncio.sleep(0.15)
    rest = [response async for response in stream]
    assert [first.completion_text] + [item.completion_text for item in rest] == [
        str(i) for i in range(8)
    ] + ["ok"]


async def test_final_adapter_error_keeps_partial(bundle):
    bundle.responses.raw["output"] = [
        {"type": "image_generation_call", "id": "i", "result": "test"}
    ]
    stream = provider(bundle).text_chat_stream(prompt="hi")
    with pytest.raises(ProtocolError) as error:
        [item async for item in stream]
    assert error.value.partial


async def test_sdk_image_timeout_zero_is_not_defaulted(bundle):
    p = provider(bundle)
    with pytest.raises(Exception) as error:
        await p.generate_image("test", timeout=0)
    assert isinstance(
        error.value,
        __import__(
            "astrbot_plugin_grok_oauth.grok_oauth.errors", fromlist=["InvalidRequest"]
        ).InvalidRequest,
    )


async def test_sdk_deadline_covers_reference_import(bundle):
    entered = asyncio.Event()

    async def slow_import(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    bundle.assets = SimpleNamespace(import_reference=slow_import)
    bundle.allowed_roots = ()
    p = provider(bundle)
    start = time.monotonic()
    with pytest.raises(ProtocolError):
        await asyncio.wait_for(
            p.generate_image("edit", reference_images=["fixture"], timeout=0.03), 0.5
        )
    assert entered.is_set() and time.monotonic() - start < 0.3


def test_named_source_template_registration_preserves_foreign_entry():
    from astrbot_plugin_grok_oauth.astrbot_adapter.compat import provider_source_templates

    templates = provider_source_templates()
    foreign = {"id": "other", "type": "other"}
    templates["Grok Oauth"] = foreign
    try:
        with pytest.raises(RegistrationConflict):
            register_provider("collision-test", GrokOAuthProvider)
        assert templates["Grok Oauth"] is foreign
        assert TYPE not in provider_cls_map
    finally:
        del templates["Grok Oauth"]


def test_named_source_template_cleanup_only_removes_owned_identity():
    from astrbot_plugin_grok_oauth.astrbot_adapter.compat import provider_source_templates

    templates = provider_source_templates()
    before = dict(templates)
    handle = register_provider("normal-cleanup", GrokOAuthProvider)
    unregister_provider(handle)
    assert templates == before
    handle = register_provider("foreign-replacement", GrokOAuthProvider)
    foreign = {"id": "replacement", "type": "other"}
    templates["Grok Oauth"] = foreign
    try:
        unregister_provider(handle)
        assert templates["Grok Oauth"] is foreign
        assert TYPE not in provider_cls_map
    finally:
        del templates["Grok Oauth"]


def test_upgrade_removes_only_known_legacy_source_template():
    from astrbot.dashboard.services.config_service import ProviderConfigService
    from astrbot_plugin_grok_oauth.astrbot_adapter.compat import (
        ProviderType,
        provider_source_templates,
        register_provider_adapter,
    )

    templates = provider_source_templates()
    legacy = {
        "id": TYPE,
        "type": TYPE,
        "provider_type": "chat_completion",
        "enable": False,
        "key": ["oauth-managed"],
        "api_base": "https://api.x.ai/v1",
        "model": "grok-4.6",
        "grok_account_slot": "default",
        "timeout": 180,
    }
    assert TYPE not in templates
    register_provider_adapter(
        TYPE, "legacy", ProviderType.CHAT_COMPLETION, default_config_tmpl=legacy
    )(GrokOAuthProvider)
    old = provider_cls_map[TYPE]
    service = ProviderConfigService(
        SimpleNamespace(astrbot_config={"provider": []}, provider_manager=None)
    )
    handle = None
    try:
        service.get_provider_schema()
        assert templates[TYPE] is legacy  # Actual current core keeps this shallow reference.
        del provider_cls_map[TYPE]
        provider_registry.remove(old)  # v0.1.0 unregister did not clean this injected template.
        handle = register_provider("upgrade-test", GrokOAuthProvider)
        schema = service.get_provider_schema()["config_schema"]["provider"]["config_template"]
        assert [name for name, item in schema.items() if item.get("type") == TYPE] == ["Grok Oauth"]
    finally:
        if handle:
            unregister_provider(handle)
        if provider_cls_map.get(TYPE) is old:
            del provider_cls_map[TYPE]
        if old in provider_registry:
            provider_registry.remove(old)
        if templates.get(TYPE) is legacy:
            del templates[TYPE]


def test_unknown_legacy_template_is_not_removed_or_overwritten():
    from astrbot_plugin_grok_oauth.astrbot_adapter.compat import provider_source_templates

    templates = provider_source_templates()
    foreign = {"id": "foreign", "type": TYPE}
    templates[TYPE] = foreign
    handle = None
    try:
        with pytest.raises(RegistrationConflict):
            handle = register_provider("unexpected-template", GrokOAuthProvider)
        assert templates[TYPE] is foreign
        assert "Grok Oauth" not in templates
    finally:
        if handle:
            unregister_provider(handle)
        del templates[TYPE]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {"oauth_web_search": "disabled"},
        {"retry_rate_limits": False},
        {"oauth_web_search": "inherit", "retry_rate_limits": True},
        {"oauth_web_search": "disabled", "retry_rate_limits": False},
    ],
)
async def test_host_request_policies_never_enter_model_payload(bundle, streaming, options):
    p = provider(bundle)
    if streaming:
        result = [item async for item in p.text_chat_stream(prompt="policy", **options)]
        assert result[-1].completion_text == "ok"
    else:
        assert (await p.text_chat(prompt="policy", **options)).completion_text == "ok"
    assert len(bundle.responses.calls) == 1
    payload = bundle.responses.calls[0][0]
    assert "oauth_web_search" not in payload and "retry_rate_limits" not in payload
    assert "tools" not in payload


@pytest.mark.parametrize("streaming", [False, True])
async def test_real_http_429_preserves_host_status_without_retry(bundle, streaming):
    import httpx
    from astrbot.core.provider.sources.request_retry import (
        _is_retryable_provider_request_error,
    )
    from astrbot_plugin_grok_oauth.grok_oauth.errors import RateLimited
    from astrbot_plugin_grok_oauth.grok_oauth.http import AuthorizedHttp
    from astrbot_plugin_grok_oauth.grok_oauth.models import TokenSnapshot
    from astrbot_plugin_grok_oauth.grok_oauth.responses import ResponsesClient

    calls = []

    class OAuth:
        async def get_token(self, **kwargs):
            return TokenSnapshot("default", "synthetic", "synthetic", None, "s", "c")

    def wire(request):
        calls.append(request)
        return httpx.Response(
            429, json={"error": "sensitive-body"}, headers={"x-request-id": "limit-1"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        bundle.responses = ResponsesClient(AuthorizedHttp(client, OAuth()))
        p = provider(bundle)
        with pytest.raises(RateLimited) as caught:
            if streaming:
                [
                    item
                    async for item in p.text_chat_stream(
                        prompt="limit", oauth_web_search="disabled", retry_rate_limits=False
                    )
                ]
            else:
                await p.text_chat(
                    prompt="limit", oauth_web_search="disabled", retry_rate_limits=False
                )
    assert len(calls) == 1
    assert caught.value.status_code == 429
    assert not _is_retryable_provider_request_error(caught.value, retry_rate_limits=False)
    assert _is_retryable_provider_request_error(caught.value, retry_rate_limits=True)
    assert caught.value.request_id == "limit-1"
    assert "sensitive-body" not in str(caught.value)


@pytest.mark.parametrize("streaming", [False, True])
async def test_inherited_search_policy_and_explicit_override(bundle, streaming, host_search_policy):
    provider_oauth_web_search = host_search_policy

    token = provider_oauth_web_search.set("live")
    bundle.config["search_enabled"] = False
    p = provider(bundle)
    try:
        with pytest.raises(PermissionDenied, match="Grok web search"):
            if streaming:
                [item async for item in p.text_chat_stream(prompt="live")]
            else:
                await p.text_chat(prompt="live")
        assert bundle.responses.calls == []
        if streaming:
            results = [
                item async for item in p.text_chat_stream(prompt="off", oauth_web_search="disabled")
            ]
            assert results[-1].completion_text == "ok"
        else:
            assert (
                await p.text_chat(prompt="off", oauth_web_search="disabled")
            ).completion_text == "ok"
        assert provider_oauth_web_search.get() == "live"
    finally:
        provider_oauth_web_search.reset(token)


@pytest.mark.parametrize(
    "options, error",
    [
        ({"retry_rate_limits": "false"}, "InvalidRequest"),
        ({"retry_rate_limits": 0}, "InvalidRequest"),
        ({"oauth_web_search": []}, "InvalidRequest"),
        ({"oauth_web_search": "unknown"}, "InvalidRequest"),
        ({"oauth_web_search": "cached"}, "UnsupportedModelParameter"),
    ],
)
async def test_invalid_or_unavailable_host_policy_fails_before_network(bundle, options, error):
    from astrbot_plugin_grok_oauth.grok_oauth.errors import GrokOAuthError

    with pytest.raises(GrokOAuthError) as caught:
        await provider(bundle).text_chat(prompt="policy", **options)
    assert caught.value.code == error
    assert bundle.responses.calls == []


@pytest.mark.parametrize(
    "model", ["grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5", "grok-4.3"]
)
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_modalities_keep_image_request_on_grok(bundle, model, missing):
    from astrbot.core.astr_main_agent import _select_image_chat_provider
    from astrbot.core.provider.entities import ProviderRequest

    config = {"type": TYPE, "id": "legacy-grok", "model": model}
    if not missing:
        config["modalities"] = None
    original = copy.deepcopy(config)
    p = GrokOAuthProvider(config, {})
    fallback = SimpleNamespace(provider_config={"id": "fallback", "modalities": ["image"]})
    req = ProviderRequest(prompt="Describe the image", image_urls=["data:image/png;base64,AAAA"])
    assert _select_image_chat_provider(p, req, [fallback]) is p
    assert p.provider_config["modalities"] == ["text", "image", "tool_use"]
    assert config == original
    assert bundle.responses.calls == []
    await p.terminate()


@pytest.mark.parametrize(
    "modalities", [[], ["text"], ["text", "image"], ["text", "image", "audio", "tool_use"]]
)
async def test_explicit_modalities_preserve_core_routing(bundle, modalities):
    from astrbot.core.astr_main_agent import _select_image_chat_provider
    from astrbot.core.provider.entities import ProviderRequest

    p = provider(bundle, modalities=modalities)
    fallback = SimpleNamespace(provider_config={"id": "fallback", "modalities": ["image"]})
    req = ProviderRequest(prompt="Describe the image", image_urls=["data:image/png;base64,AAAA"])
    expected = p if not modalities or "image" in modalities else fallback
    assert _select_image_chat_provider(p, req, [fallback]) is expected
    assert p.provider_config["modalities"] == modalities
    await p.terminate()


async def test_unknown_model_does_not_gain_unverified_vision(bundle):
    from astrbot.core.astr_main_agent import _provider_supports_modality

    p = provider(bundle, model="grok-future-unknown")
    assert not _provider_supports_modality(p, "image")
    assert "modalities" not in p.provider_config
    await p.terminate()


@pytest.mark.parametrize("model", ["grok-4.7", "grok-4.7-build-fast"])
@pytest.mark.parametrize("stream", [False, True])
async def test_47_reasoning_effort_reaches_both_response_paths(bundle, model, stream):
    p = provider(bundle, model=model, reasoning_effort="xhigh")
    if stream:
        [part async for part in p.text_chat_stream(prompt="test")]
    else:
        await p.text_chat(prompt="test")
    payload = bundle.responses.calls[-1][0]
    assert payload["model"] == model
    assert payload["reasoning"] == {"effort": "xhigh"}
    assert payload["store"] is False
    await p.terminate()


@pytest.mark.parametrize("model", ["grok-4.7", "grok-4.7-build-fast"])
@pytest.mark.parametrize("stream", [False, True])
async def test_47_mixed_tools_state_survives_host_history_roundtrip(bundle, model, stream):
    from astrbot.core.agent.message import Message, TextPart, ThinkPart, ToolCall
    from astrbot.core.provider.entities import ToolCallsResult

    p = provider(bundle, model=model)
    output = [
        {"type": "reasoning", "encrypted_content": "before-search"},
        {
            "type": "web_search_call",
            "id": "s1",
            "status": "completed",
            "action": {"type": "search"},
            "encrypted_content": "search-state",
        },
        {"type": "reasoning", "summary": None, "encrypted_content": "after-search"},
        {"type": "function_call", "call_id": "call1", "name": "f", "arguments": "{}"},
    ]
    bundle.responses.raw["output"] = copy.deepcopy(output)
    tools = ToolSet(
        [
            FunctionTool(
                name="f", description="test", parameters={"type": "object", "properties": {}}
            )
        ]
    )
    kwargs = dict(prompt="look up and call f", func_tool=tools, oauth_web_search="live")
    if stream:
        result = [c async for c in p.text_chat_stream(**kwargs)][-1]
    else:
        result = await p.text_chat(**kwargs)
    saved = ToolCallsResult(
        tool_calls_info=Message(
            role="assistant",
            content=[ThinkPart(think="", encrypted=result.reasoning_signature)]
            + ([TextPart(text=result.completion_text)] if result.completion_text else []),
            tool_calls=[
                ToolCall(id="call1", function=ToolCall.FunctionBody(name="f", arguments="{}"))
            ],
        ),
        tool_calls_result=[Message(role="tool", tool_call_id="call1", content="done")],
    ).to_openai_messages()
    restored = json.loads(json.dumps(saved))
    bundle.responses.raw["output"] = [
        {"type": "message", "content": [{"type": "output_text", "text": "finished"}]}
    ]
    await p.text_chat(prompt="continue", contexts=restored)
    sent = bundle.responses.calls[-1][0]["input"]
    assert sent[: len(output)] == output
    assert sent[len(output)] == {
        "type": "function_call_output",
        "call_id": "call1",
        "output": "done",
    }
    assert len([item for item in sent if item.get("type") == "function_call"]) == 1
    await p.terminate()


@pytest.mark.parametrize("stream", [False, True])
async def test_native_model_extra_body_reaches_request_and_call_options_win(bundle, stream):
    p = provider(
        bundle,
        model="grok-4.7",
        custom_extra_body={"temperature": 0.4, "max_tokens": 321, "reasoning_effort": "low"},
    )

    async def run(**kwargs):
        if stream:
            return [c async for c in p.text_chat_stream(prompt="hello", **kwargs)]
        return await p.text_chat(prompt="hello", **kwargs)

    await run()
    payload = bundle.responses.calls[-1][0]
    assert payload["temperature"] == 0.4 and payload["max_output_tokens"] == 321
    assert payload["reasoning"] == {"effort": "low"} and "max_tokens" not in payload
    await run(temperature=0.1, max_output_tokens=123, reasoning={"effort": "high"})
    payload = bundle.responses.calls[-1][0]
    assert payload["temperature"] == 0.1 and payload["max_output_tokens"] == 123
    assert payload["reasoning"] == {"effort": "high"}
    await p.terminate()


@pytest.mark.parametrize(
    "extra",
    [
        {"store": True},
        {"model": "other"},
        {"input": []},
        {"tools": []},
        {"temperature": False},
        {"max_tokens": 10, "max_output_tokens": 20},
        ["bad"],
    ],
)
async def test_native_extra_body_cannot_override_control_or_bypass_validation(bundle, extra):
    p = provider(bundle, custom_extra_body=extra)
    with pytest.raises(UnsupportedModelParameter):
        await p.text_chat(prompt="hello")
    assert bundle.responses.calls == []
    await p.terminate()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("ending", ["deadline", "caller_timeout", "explicit_cancel", "success"])
async def test_real_provider_transport_termination_diagnostics(bundle, streaming, ending):
    import httpx
    from astrbot_plugin_grok_oauth.grok_oauth.http import AuthorizedHttp
    from astrbot_plugin_grok_oauth.grok_oauth.models import TokenSnapshot
    from astrbot_plugin_grok_oauth.grok_oauth.responses import ResponsesClient

    entered = asyncio.Event()
    cleaned = asyncio.Event()
    records = []
    attempts = 0

    class OAuth:
        async def get_token(self, **kwargs):
            return TokenSnapshot("test", "private-token", "private-refresh", None, "test", "test")

    class Logger:
        def info(self, template, value):
            records.append(json.loads(value))

    async def upstream(request):
        nonlocal attempts
        attempts += 1
        entered.set()
        if ending != "success":
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        raw = {
            "id": "diagnostic-result",
            "model": "grok-4.7",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        }
        if streaming:
            event = {"type": "response.completed", "response": raw}
            return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n")
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        bundle.http = AuthorizedHttp(
            client, OAuth(), diagnostic_logging=True, diagnostic_logger=Logger()
        )
        bundle.responses = ResponsesClient(bundle.http)
        p = provider(bundle, model="grok-4.7")

        async def call():
            timeout = 0.05 if ending == "deadline" else 5
            if streaming:
                return [
                    item
                    async for item in p.text_chat_stream(prompt="private-prompt", timeout=timeout)
                ]
            return await p.text_chat(prompt="private-prompt", timeout=timeout)

        if ending == "success":
            result = await call()
            assert (result[-1] if streaming else result).completion_text == "ok"
        elif ending == "deadline":
            with pytest.raises(StreamIncomplete if streaming else TimeoutError):
                await call()
        elif ending == "caller_timeout":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(call(), timeout=0.05)
        else:
            task = asyncio.create_task(call())
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await p.terminate()

    transport_records = [record for record in records if "operation" in record]
    assert len(transport_records) == 1
    record = transport_records[0]
    assert attempts == 1
    assert "private-token" not in json.dumps(records) and "private-prompt" not in json.dumps(
        records
    )
    if ending == "deadline":
        assert record["outcome"] == "timeout"
        assert record["termination_reason"] == "request_deadline"
        assert record["remaining_ms"] == 0
    elif ending in {"caller_timeout", "explicit_cancel"}:
        assert record["outcome"] == "cancelled"
        assert record["termination_reason"] == "external_cancel"
        assert record["remaining_ms"] > 0
    else:
        assert record["outcome"] == ("scope_returned" if streaming else "success")
        assert record["termination_reason"] == ""
    if ending != "success":
        assert cleaned.is_set()
    assert not p._tasks


@pytest.mark.parametrize("streaming", [False, True])
async def test_stock_host_without_search_policy_keeps_normal_chat(bundle, streaming, monkeypatch):
    from astrbot.core.provider.sources import request_retry

    monkeypatch.delattr(request_retry, "provider_oauth_web_search", raising=False)
    bundle.config["search_enabled"] = False
    p = provider(bundle)
    if streaming:
        result = [item async for item in p.text_chat_stream(prompt="hello")][-1]
    else:
        result = await p.text_chat(prompt="hello")
    assert result.completion_text == "ok"
    payload, _ = bundle.responses.calls[0]
    assert not any(tool.get("type") == "web_search" for tool in payload.get("tools", []))
