import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_grok_oauth.astrbot_adapter.provider import GrokOAuthProvider
from astrbot_plugin_grok_oauth.astrbot_adapter.registration import (
    register_provider,
    unregister_provider,
)
from astrbot_plugin_grok_oauth.astrbot_adapter.registry_state import bind_runtime, clear_runtime
from astrbot_plugin_grok_oauth.astrbot_adapter.runtime import GrokRuntime
from astrbot_plugin_grok_oauth.astrbot_adapter.search_tools import SearchToolService
from astrbot_plugin_grok_oauth.grok_oauth.errors import PermissionDenied, UnsupportedModelParameter
from astrbot_plugin_grok_oauth.grok_oauth.models import TokenSnapshot


class SearchEvent(SimpleNamespace):
    __hash__ = object.__hash__
    __eq__ = object.__eq__


def output(text="answer", *, search=False, function=False):
    items = []
    if search:
        items.append(
            {
                "id": "ws",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "facts"},
            }
        )
    if function:
        items.append(
            {
                "type": "function_call",
                "call_id": "search-call",
                "name": "grok_web_search",
                "arguments": '{"query":"latest facts"}',
            }
        )
    else:
        items.append(
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/news",
                                "title": "News",
                            }
                        ]
                        if search
                        else [],
                    }
                ],
            }
        )
    return {
        "id": "r",
        "model": "grok-4.6",
        "status": "completed",
        "output": items,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.fixture
async def search_env(tmp_path):
    calls = []
    behavior = {"need_search": False}

    def wire(request):
        payload = json.loads(request.content)
        calls.append(payload)
        native = any(tool.get("type") == "web_search" for tool in payload.get("tools", []))
        returned = any(item.get("type") == "function_call_output" for item in payload["input"])
        raw = output(
            "evidence" if native else "done",
            search=native,
            function=behavior["need_search"] and not native and not returned,
        )
        if payload.get("stream"):
            return httpx.Response(
                200,
                content=(
                    "data: " + json.dumps({"type": "response.completed", "response": raw}) + "\n\n"
                ).encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=raw)

    runtime = GrokRuntime({}, tmp_path, transport=httpx.MockTransport(wire))
    await runtime.open()
    await runtime.oauth.bind(
        TokenSnapshot(
            "default",
            "synthetic-access",
            "synthetic-refresh",
            None,
            "api:access",
            runtime.client_id,
        ),
        expected_epoch=runtime.oauth.epoch,
    )
    bind_runtime(runtime)
    handle = register_provider(runtime.owner_id, GrokOAuthProvider)
    provider = GrokOAuthProvider(
        {"type": "grok_oauth_chat_completion", "id": "grok-test", "model": "grok-4.6"}, {}
    )
    selected = {"provider": provider}

    async def current(umo):
        return selected["provider"]

    context = SimpleNamespace(
        get_using_provider_async=current,
        get_provider_by_id=lambda id: provider if id == "grok-test" else None,
    )
    extra = {}
    event = SearchEvent(
        unified_msg_origin="test:GroupMessage:1",
        get_extra=lambda key, default=None: extra.get(key, default),
        set_extra=lambda key, value: extra.__setitem__(key, value),
    )
    service = SearchToolService(context, runtime)
    yield SimpleNamespace(
        runtime=runtime,
        provider=provider,
        service=service,
        event=event,
        selected=selected,
        extra=extra,
        calls=calls,
        behavior=behavior,
    )
    await service.close()
    await runtime.close()
    unregister_provider(handle)
    clear_runtime(runtime)


def request_tools(env, **kwargs):
    req = ProviderRequest(prompt="hello", func_tool=ToolSet(env.service.build_tools()))
    for key, value in kwargs.items():
        setattr(req, key, value)
    return req


async def test_tool_is_distinct_and_preparation_has_no_network(search_env):
    env = search_env
    codex = FunctionTool(
        name="codex_web_search",
        description="other",
        parameters={"type": "object", "properties": {}},
    )
    req = request_tools(env)
    req.func_tool.add_tool(codex)
    original = req.func_tool
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == ["grok_web_search", "codex_web_search"]
    assert req.func_tool is not original
    assert req.func_tool.get_tool("codex_web_search") is codex and codex.active
    assert env.calls == []
    empty = ProviderRequest(prompt="intent")
    await env.service.prepare(env.event, empty)
    assert empty.func_tool is None and env.calls == []


@pytest.mark.parametrize("mode", ["disabled", "cached", "live"])
async def test_request_policy_removes_duplicate_or_forbidden_local_search(search_env, mode):
    env = search_env
    req = request_tools(env, oauth_web_search=mode)
    original = req.func_tool
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == [] and original.names() == ["grok_web_search"]
    assert env.calls == []


async def test_non_grok_hidden_unless_explicitly_configured(search_env):
    env = search_env
    env.selected["provider"] = SimpleNamespace(provider_config={"id": "codex"})
    req = request_tools(env)
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == []
    env.runtime.config["search_provider_id"] = "grok-test"
    req = request_tools(env)
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == ["grok_web_search"]
    assert env.calls == []


async def test_selected_grok_overrides_unrelated_default_and_request_remains_bound(search_env):
    env = search_env
    env.selected["provider"] = SimpleNamespace(provider_config={"id": "codex"})
    env.extra["selected_provider"] = "grok-test"
    req = request_tools(env)
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == ["grok_web_search"]
    req.oauth_web_search = "disabled"
    result = json.loads(await req.func_tool.tools[0].handler(env.event, query="facts"))
    assert result["error"] == "PermissionDenied" and env.calls == []


async def test_direct_search_and_callers_disabled_cached(search_env):
    env = search_env
    result = await env.provider.search_web("facts")
    assert result.search_calls == 1 and result.citations[0]["url"] == "https://example.com/news"
    assert len(env.calls) == 1
    with pytest.raises(PermissionDenied):
        await env.provider.search_web("facts", oauth_web_search="disabled")
    with pytest.raises(UnsupportedModelParameter):
        await env.provider.search_web("facts", oauth_web_search="cached")
    assert len(env.calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_explicit_live_native_search_preserves_other_functions_and_citations(
    search_env, streaming
):
    env = search_env
    tools = ToolSet(env.service.build_tools())
    tools.add_tool(
        FunctionTool(
            name="local_lookup",
            description="local",
            parameters={"type": "object", "properties": {}},
        )
    )
    if streaming:
        result = [
            x
            async for x in env.provider.text_chat_stream(
                prompt="facts", func_tool=tools, oauth_web_search="live"
            )
        ]
        final = result[-1]
    else:
        final = await env.provider.text_chat(
            prompt="facts", func_tool=tools, oauth_web_search="live"
        )
    assert "https://example.com/news" in final.completion_text
    sent = env.calls[0]
    assert sent["tools"][-1] == {"type": "web_search"} and sent["tool_choice"] == "auto"
    assert [t["name"] for t in sent["tools"] if t["type"] == "function"] == ["local_lookup"]
    assert tools.names() == ["grok_web_search", "local_lookup"]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("need_search", [False, True])
async def test_actual_agent_uses_search_only_when_it_calls_the_tool(
    search_env, streaming, need_search
):
    env = search_env
    env.behavior["need_search"] = need_search
    req = request_tools(env)
    await env.service.prepare(env.event, req)
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=env.provider,
        request=req,
        run_context=ContextWrapper(context=SimpleNamespace(event=env.event), tool_call_timeout=10),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks(),
        streaming=streaming,
    )
    [item async for item in runner.step_until_done(max_step=4)]
    assert runner.get_final_llm_resp().completion_text == "done"
    searches = [c for c in env.calls if any(t["type"] == "web_search" for t in c.get("tools", []))]
    assert len(searches) == int(need_search)
    assert len(env.calls) == (3 if need_search else 1)
    if need_search:
        returned = [i for i in env.calls[-1]["input"] if i.get("type") == "function_call_output"]
        data = json.loads(returned[0]["output"])
        assert (
            data["provider"] == "grok_oauth"
            and data["citations"][0]["url"] == "https://example.com/news"
        )


async def test_selected_provider_id_uses_the_explicit_grok_source(search_env):
    env = search_env
    env.selected["provider"] = SimpleNamespace(provider_config={"id": "codex"})
    env.extra["selected_provider"] = "grok-test"
    req = request_tools(env)
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == ["grok_web_search"]
    env.behavior["need_search"] = True
    response = await env.provider.text_chat(prompt="facts", func_tool=req.func_tool)
    result = json.loads(
        await req.func_tool.tools[0].handler(env.event, **response.tools_call_args[0])
    )
    assert result["status"] == "success" and result["provider_id"] == "grok-test"


async def test_search_global_disable_and_capability_snapshot(search_env):
    env = search_env
    env.runtime.config["search_enabled"] = False
    req = request_tools(env)
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == []
    assert env.provider.capabilities["web_search"]["implementation"]
    assert not env.provider.capabilities["web_search"]["enabled"]
    with pytest.raises(PermissionDenied):
        await env.provider.search_web("facts")
    with pytest.raises(PermissionDenied):
        await env.provider.text_chat(prompt="facts", oauth_web_search="live")
    assert env.calls == []


async def test_parallel_request_policies_do_not_change_global_tool(search_env):
    env = search_env
    global_tools = ToolSet(env.service.build_tools())
    enabled = ProviderRequest(prompt="facts", func_tool=global_tools)
    disabled = ProviderRequest(prompt="intent", func_tool=global_tools)
    disabled.oauth_web_search = "disabled"
    await asyncio.gather(
        env.service.prepare(env.event, enabled), env.service.prepare(env.event, disabled)
    )
    assert global_tools.names() == ["grok_web_search"] and global_tools.tools[0].active
    assert enabled.func_tool.names() == ["grok_web_search"] and disabled.func_tool.names() == []
    assert env.calls == []


async def test_search_close_and_timeout_are_bounded(search_env):
    env = search_env
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    env.runtime.search.search = slow
    from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError

    with pytest.raises(ProtocolError, match="timed out"):
        await env.provider.search_web("facts", timeout=0.01)
    assert entered.is_set() and cancelled.is_set()
    entered.clear()
    cancelled.clear()
    task = asyncio.create_task(env.provider.search_web("facts"))
    await entered.wait()
    await env.provider.terminate()
    assert task.cancelled() and cancelled.is_set()
    await env.service.close()
    result = json.loads(await env.service.search(env.event, query="facts"))
    assert result["status"] == "error" and env.calls == []


@pytest.mark.parametrize("inline", [False, True])
async def test_real_stream_delivery_includes_terminal_search_sources(
    search_env, inline, host_search_policy
):
    from astrbot.core.astr_agent_run_util import run_agent

    env = search_env
    token = host_search_policy.set("live")
    text = "evidence" + (" https://example.com/news" if inline else "")

    def wire(request):
        events = [
            {"type": "response.output_text.delta", "delta": text},
            {"type": "response.completed", "response": output(text, search=True)},
        ]
        return httpx.Response(
            200,
            content="".join("data: " + json.dumps(e) + "\n\n" for e in events).encode(),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        original = env.runtime.http._client
        env.runtime.http._client = client
        env.event.is_stopped = lambda: False
        env.event.get_platform_name = lambda: "test"
        env.event.get_platform_id = lambda: "test"
        env.event.trace = SimpleNamespace(record=lambda *args, **kwargs: None)
        runner = ToolLoopAgentRunner()
        try:
            await runner.reset(
                provider=env.provider,
                request=ProviderRequest(prompt="facts"),
                run_context=ContextWrapper(
                    context=SimpleNamespace(event=env.event), tool_call_timeout=10
                ),
                tool_executor=FunctionToolExecutor(),
                agent_hooks=BaseAgentRunHooks(),
                streaming=True,
            )
            chunks = [chunk async for chunk in run_agent(runner, max_step=3)]
            delivered = "".join(chunk.get_plain_text() for chunk in chunks)
            assert delivered.startswith("evidence")
            assert delivered.count("https://example.com/news") == 1
            assert delivered == runner.get_final_llm_resp().completion_text
        finally:
            env.runtime.http._client = original
            host_search_policy.reset(token)


async def test_unbound_tool_respects_current_disabled_policy(search_env, host_search_policy):
    provider_oauth_web_search = host_search_policy

    env = search_env
    token = provider_oauth_web_search.set("disabled")
    try:
        result = json.loads(await env.service.search(env.event, query="facts"))
        assert result["error"] == "PermissionDenied" and env.calls == []
    finally:
        provider_oauth_web_search.reset(token)


@pytest.mark.parametrize("streaming", [False, True])
async def test_disabled_search_rejects_unrequested_search_function_from_model(
    search_env, streaming
):
    env = search_env
    env.behavior["need_search"] = True
    tools = ToolSet(env.service.build_tools())
    with pytest.raises(PermissionDenied):
        if streaming:
            [
                x
                async for x in env.provider.text_chat_stream(
                    prompt="facts", func_tool=tools, oauth_web_search="disabled"
                )
            ]
        else:
            await env.provider.text_chat(
                prompt="facts", func_tool=tools, oauth_web_search="disabled"
            )
    assert len(env.calls) == 1
    assert "tools" not in env.calls[0]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cross_model_enabled", [False, True])
async def test_fallback_model_needs_explicit_search_permission(
    search_env, streaming, cross_model_enabled
):
    from astrbot.core.pipeline.process_stage.follow_up import (
        register_active_runner,
        unregister_active_runner,
    )
    from astrbot.core.provider.entities import LLMResponse

    env = search_env
    if cross_model_enabled:
        env.runtime.config["search_provider_id"] = "grok-test"

    class ForeignProvider:
        provider_config = {"id": "foreign", "model": "foreign-model"}
        results = []

        def get_model(self):
            return "foreign-model"

        async def text_chat(self, **kwargs):
            tool_results = [
                item
                for item in env.provider._ensure_message_to_dicts(kwargs.get("contexts", []))
                if item.get("role") == "tool"
            ]
            if tool_results:
                self.results.append(json.loads(tool_results[-1]["content"]))
                return LLMResponse("assistant", completion_text="done")
            return LLMResponse(
                "tool",
                tools_call_name=["grok_web_search"],
                tools_call_args=[{"query": "facts"}],
                tools_call_ids=["fallback-search"],
            )

        async def text_chat_stream(self, **kwargs):
            yield await self.text_chat(**kwargs)

    foreign = ForeignProvider()
    requests = []

    def wire(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if any(t.get("type") == "web_search" for t in payload.get("tools", [])):
            return httpx.Response(200, json=output("evidence", search=True))
        return httpx.Response(503, json={"error": "temporary"})

    req = request_tools(env)
    await env.service.prepare(env.event, req)
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=env.provider,
        request=req,
        run_context=ContextWrapper(context=SimpleNamespace(event=env.event), tool_call_timeout=10),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks(),
        streaming=streaming,
    )
    runner.fallback_providers = [foreign]
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        original = env.runtime.http._client
        env.runtime.http._client = client
        register_active_runner(env.event.unified_msg_origin, runner)
        try:
            [item async for item in runner.step_until_done(max_step=4)]
            assert runner.get_final_llm_resp().completion_text == "done"
            assert len(foreign.results) == 1
            assert foreign.results[0]["status"] == ("success" if cross_model_enabled else "error")
            assert len(requests) == (2 if cross_model_enabled else 1)
        finally:
            unregister_active_runner(env.event.unified_msg_origin, runner)
            env.runtime.http._client = original


async def test_other_active_event_does_not_change_current_tool_scope(search_env):
    from astrbot.core.pipeline.process_stage.follow_up import (
        register_active_runner,
        unregister_active_runner,
    )

    env = search_env
    other_event = SearchEvent(unified_msg_origin=env.event.unified_msg_origin)
    runner = SimpleNamespace(
        run_context=SimpleNamespace(context=SimpleNamespace(event=other_event)),
        provider=SimpleNamespace(provider_config={"id": "foreign"}),
        request_stop=lambda: None,
    )
    register_active_runner(other_event.unified_msg_origin, runner)
    try:
        req = request_tools(env)
        await env.service.prepare(env.event, req)
        assert req.func_tool.names() == ["grok_web_search"]
        result = json.loads(await req.func_tool.tools[0].handler(other_event, query="facts"))
        assert result["error"] == "PermissionDenied" and env.calls == []
    finally:
        unregister_active_runner(other_event.unified_msg_origin, runner)


async def test_missing_runner_registry_keeps_non_grok_tools_hidden(search_env, monkeypatch):
    from astrbot.core.pipeline.process_stage import follow_up

    monkeypatch.delattr(follow_up, "_ACTIVE_AGENT_RUNNERS", raising=False)
    env = search_env
    env.selected["provider"] = SimpleNamespace(provider_config={"id": "other"})
    req = request_tools(env)
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == []
    assert env.calls == []
    env.extra["selected_provider"] = "grok-test"
    req = request_tools(env)
    await env.service.prepare(env.event, req)
    assert req.func_tool.names() == ["grok_web_search"]
    assert env.calls == []
