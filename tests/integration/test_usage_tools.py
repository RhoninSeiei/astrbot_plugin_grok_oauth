import asyncio
import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_grok_oauth.astrbot_adapter import usage_tools
from astrbot_plugin_grok_oauth.astrbot_adapter.provider import GrokOAuthProvider
from astrbot_plugin_grok_oauth.astrbot_adapter.registration import (
    register_provider,
    unregister_provider,
)
from astrbot_plugin_grok_oauth.astrbot_adapter.registry_state import bind_runtime, clear_runtime
from astrbot_plugin_grok_oauth.astrbot_adapter.runtime import GrokRuntime
from astrbot_plugin_grok_oauth.grok_oauth.models import TokenSnapshot

USAGE_TOOL_NAMES = ("grok_usage_status", "grok_usage_breakdown")


@pytest.fixture
async def env(tmp_path):
    assert hasattr(usage_tools, "UsageToolService"), "usage LLM service is not implemented"
    calls = []
    tool_outputs = []
    replayed_states = []
    requested_tool = {"name": "grok_usage_status"}

    def wire(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/billing":
            return httpx.Response(
                200,
                json={
                    "config": {
                        "creditUsagePercent": 20,
                        "currentPeriod": {
                            "type": "USAGE_PERIOD_TYPE_WEEKLY",
                            "start": "2026-09-19T00:00:00Z",
                            "end": "2026-09-26T00:00:00Z",
                        },
                        "productUsage": [
                            {"product": "GrokBuild", "usagePercent": 12},
                            {"product": "GrokChat", "usagePercent": 34},
                            {"product": "GrokImagine", "usagePercent": 56},
                        ],
                    }
                },
            )
        payload = json.loads(request.content)
        returned_items = [x for x in payload["input"] if x.get("type") == "function_call_output"]
        returned = bool(returned_items)
        if returned:
            replayed_states.append([x for x in payload["input"] if x.get("type") == "reasoning"])
            tool_outputs.append(json.loads(returned_items[-1]["output"]))
        items = (
            [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]
            if returned
            else [
                {"type": "reasoning", "encrypted_content": "quota-state"},
                {
                    "type": "function_call",
                    "call_id": "usage-call",
                    "name": requested_tool["name"],
                    "arguments": "{}",
                },
            ]
        )
        raw = {"id": "r", "model": "grok-4.6", "status": "completed", "output": items}
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
    # Keep the synthetic quota period current regardless of the calendar date.
    runtime.billing._clock = lambda: datetime(2026, 9, 20, tzinfo=UTC).timestamp()
    await runtime.open()
    await runtime.oauth.bind(
        TokenSnapshot(
            "default",
            "test-access",
            "test-refresh",
            None,
            "",
            runtime.client_id,
            user_id="test-user",
        ),
        expected_epoch=0,
    )
    bind_runtime(runtime)
    reg = register_provider(runtime.owner_id, GrokOAuthProvider)
    provider = GrokOAuthProvider(
        {"id": "grok", "type": "grok_oauth_chat_completion", "model": "grok-4.6"}, {}
    )
    selected = [provider]

    async def current(umo):
        return selected[0]

    context = SimpleNamespace(
        get_using_provider_async=current,
        get_provider_by_id=lambda id: provider if id == "grok" else None,
    )
    access = {"admin": True, "private": True}
    event = SimpleNamespace(
        unified_msg_origin="test:FriendMessage:1",
        get_extra=lambda key, default=None: default,
        is_admin=lambda: access["admin"],
        is_private_chat=lambda: access["private"],
    )
    service = usage_tools.UsageToolService(context, runtime)
    yield SimpleNamespace(
        runtime=runtime,
        provider=provider,
        service=service,
        event=event,
        selected=selected,
        access=access,
        calls=calls,
        tool_outputs=tool_outputs,
        replayed_states=replayed_states,
        requested_tool=requested_tool,
    )
    await service.close()
    await runtime.close()
    unregister_provider(reg)
    clear_runtime(runtime)


async def prepared(env):
    req = ProviderRequest(prompt="usage", func_tool=ToolSet(env.service.build_tools()))
    await env.service.prepare(env.event, req)
    return req


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
@pytest.mark.parametrize("mode", ["full", "skills_like"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_runner_empty_arguments_usage(env, tool_name, mode, streaming):
    env.requested_tool["name"] = tool_name
    req = await prepared(env)
    assert req.func_tool.names() == list(USAGE_TOOL_NAMES)
    schemas = {item["function"]["name"]: item["function"] for item in req.func_tool.openai_schema()}
    assert schemas[tool_name]["parameters"]["properties"] == {}
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=env.provider,
        request=req,
        run_context=ContextWrapper(context=SimpleNamespace(event=env.event), tool_call_timeout=5),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks(),
        streaming=streaming,
        tool_schema_mode=mode,
    )
    [item async for item in runner.step_until_done(max_step=4)]
    assert runner.get_final_llm_resp().completion_text == "done"
    assert env.calls.count("/v1/billing") == 1
    assert env.replayed_states[-1] == [{"type": "reasoning", "encrypted_content": "quota-state"}]
    output = env.tool_outputs[-1]
    if tool_name == "grok_usage_status":
        assert output["used_percent"] == 20 and "products" not in output
    else:
        assert output["percent_basis"] == "upstream_product_usage"
        assert [item["product"] for item in output["products"]] == [
            "GrokBuild",
            "GrokChat",
            "GrokImagine",
        ]
        assert "used_percent" not in output


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
async def test_empty_call_proof_is_one_use_and_json_replay_cannot_authorize(env, tool_name):
    env.requested_tool["name"] = tool_name
    req = await prepared(env)
    result = await env.provider.text_chat(prompt="usage", func_tool=req.func_tool)
    name = result.tools_call_name[0]
    assert name == tool_name and result.tools_call_args == [{}]
    replay = json.loads(json.dumps(name))
    denied = json.loads(await req.func_tool.get_tool(replay).handler(env.event))
    assert denied["status"] == "denied" and "/v1/billing" not in env.calls
    copied = copy.deepcopy(name)
    allowed = json.loads(await req.func_tool.get_tool(name).handler(env.event))
    if tool_name == "grok_usage_status":
        assert allowed["used_percent"] == 20 and "products" not in allowed
    else:
        assert allowed["products"][0]["product"] == "GrokBuild"
        assert "used_percent" not in allowed
    assert json.loads(await req.func_tool.get_tool(copied).handler(env.event))["status"] == "denied"


@pytest.mark.parametrize("issued_name", USAGE_TOOL_NAMES)
async def test_usage_proof_cannot_be_disguised_as_the_other_tool(env, issued_name):
    env.requested_tool["name"] = issued_name
    req = await prepared(env)
    result = await env.provider.text_chat(prompt="usage", func_tool=req.func_tool)
    issued = result.tools_call_name[0]
    other_name = next(name for name in USAGE_TOOL_NAMES if name != issued_name)
    disguised = type(issued)(other_name, issued._proof)

    denied = json.loads(await req.func_tool.get_tool(disguised).handler(env.event))
    assert denied["status"] == "denied"
    assert "/v1/billing" not in env.calls
    allowed = json.loads(await req.func_tool.get_tool(issued).handler(env.event))
    assert allowed["status"] in {"success", "unknown", "partial"}
    assert env.calls.count("/v1/billing") == 1


async def test_status_and_breakdown_share_one_cached_billing_fetch(env):
    results = {}
    for tool_name in USAGE_TOOL_NAMES:
        env.requested_tool["name"] = tool_name
        req = await prepared(env)
        response = await env.provider.text_chat(prompt="usage", func_tool=req.func_tool)
        results[tool_name] = json.loads(
            await req.func_tool.get_tool(response.tools_call_name[0]).handler(env.event)
        )

    assert results["grok_usage_status"]["used_percent"] == 20
    assert results["grok_usage_breakdown"]["products"][0]["product"] == "GrokBuild"
    assert env.calls.count("/v1/billing") == 1


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
@pytest.mark.parametrize(
    "admin,private,allow",
    [
        (False, True, False),
        (False, False, False),
        (False, False, True),
        (True, False, False),
        (True, False, True),
    ],
)
async def test_permissions_precede_even_cached_usage(env, tool_name, admin, private, allow):
    env.requested_tool["name"] = tool_name
    req = await prepared(env)
    result = await env.provider.text_chat(prompt="usage", func_tool=req.func_tool)
    env.access.update(admin=admin, private=private)
    if allow:
        env.runtime.config["usage_group_allowlist"] = [env.event.unified_msg_origin]
    data = json.loads(await req.func_tool.get_tool(result.tools_call_name[0]).handler(env.event))
    assert data["status"] == ("success" if private or allow else "denied")
    assert env.calls.count("/v1/billing") == int(private or allow)


async def test_cross_model_is_hidden_even_with_legacy_opt_in(env):
    env.selected[0] = SimpleNamespace(provider_config={"id": "foreign"})
    env.runtime.config.update(usage_cross_model_enabled=True, usage_provider_id="grok")
    req = await prepared(env)
    assert not req.func_tool.tools
    for tool in env.service.build_tools():
        assert json.loads(await tool.handler(env.event))["status"] == "denied"
    assert env.calls == []


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cross_enabled", [False, True])
async def test_real_fallback_foreign_cannot_borrow_primary_identity(
    env, monkeypatch, tool_name, streaming, cross_enabled
):
    from astrbot.core.provider.entities import LLMResponse
    from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError

    if cross_enabled:
        env.runtime.config.update(usage_cross_model_enabled=True, usage_provider_id="grok")

    class Foreign:
        provider_config = {"id": "foreign", "model": "foreign"}
        results = []

        def get_model(self):
            return "foreign"

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
                tools_call_name=[tool_name],
                tools_call_args=[{}],
                tools_call_ids=["foreign-call"],
            )

        async def text_chat_stream(self, **kwargs):
            yield await self.text_chat(**kwargs)

    async def failed(**kwargs):
        raise ProtocolError()

    async def failed_stream(**kwargs):
        raise ProtocolError()
        yield

    monkeypatch.setattr(env.provider, "text_chat", failed)
    monkeypatch.setattr(env.provider, "text_chat_stream", failed_stream)
    req = await prepared(env)
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=env.provider,
        request=req,
        run_context=ContextWrapper(context=SimpleNamespace(event=env.event), tool_call_timeout=5),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks(),
        streaming=streaming,
    )
    foreign = Foreign()
    runner.fallback_providers = [foreign]
    [item async for item in runner.step_until_done(max_step=4)]
    assert runner.get_final_llm_resp().completion_text == "done"
    assert foreign.results[-1]["status"] == "denied"
    assert env.calls.count("/v1/billing") == 0


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
async def test_parallel_requests_share_global_set_without_handler_mutation(env, tool_name):
    env.requested_tool["name"] = tool_name
    global_tools = ToolSet(env.service.build_tools())
    original_handlers = {tool.name: tool.handler for tool in global_tools.tools}
    first = ProviderRequest(prompt="one", func_tool=global_tools)
    second = ProviderRequest(prompt="two", func_tool=global_tools)
    await env.service.prepare(env.event, first)
    other = SimpleNamespace(**vars(env.event))
    await env.service.prepare(other, second)
    results = await asyncio.gather(
        *(
            env.provider.text_chat(prompt=req.prompt, func_tool=req.func_tool)
            for req in [first, second]
        )
    )
    assert {tool.name: tool.handler for tool in global_tools.tools} == original_handlers
    handlers = [
        req.func_tool.get_tool(result.tools_call_name[0]).handler
        for req, result in zip([first, second], results)
    ]
    denied = json.loads(await handlers[0](other))
    assert denied["status"] == "denied"
    allowed = await asyncio.gather(handlers[0](env.event), handlers[1](other))
    assert all(json.loads(value)["status"] == "success" for value in allowed)
    assert env.calls.count("/v1/billing") == 1


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
async def test_unprepared_global_executor_fails_closed_even_for_grok(env, tool_name):
    env.requested_tool["name"] = tool_name
    req = ProviderRequest(prompt="usage", func_tool=ToolSet(env.service.build_tools()))
    response = await env.provider.text_chat(prompt="usage", func_tool=req.func_tool)
    tool = req.func_tool.get_tool(response.tools_call_name[0])
    assert json.loads(await tool.handler(env.event))["status"] == "denied"
    assert "/v1/billing" not in env.calls


@pytest.mark.parametrize(
    "command,expected", [("usage", "已用：20%"), ("usage_breakdown", "Grok Build：12%")]
)
async def test_direct_command_still_queries_when_chat_is_rate_limited(env, command, expected):
    from astrbot_plugin_grok_oauth.grok_oauth.errors import RateLimited
    from astrbot_plugin_grok_oauth.main import GrokOAuthPlugin

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(429))
    ) as client:
        original = env.runtime.http._client
        env.runtime.http._client = client
        try:
            with pytest.raises(RateLimited):
                await env.provider.text_chat(prompt="chat")
            env.event.plain_result = lambda text: text
            output = [
                value
                async for value in getattr(GrokOAuthPlugin, command)(
                    SimpleNamespace(runtime=env.runtime), env.event
                )
            ]
            assert expected in output[0] and env.calls == ["/v1/billing"]
        finally:
            env.runtime.http._client = original


@pytest.mark.parametrize("cross_enabled", [False, True])
@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
async def test_issued_usage_call_cannot_survive_account_rebinding(env, cross_enabled, tool_name):
    from dataclasses import replace

    if cross_enabled:
        env.runtime.config.update(usage_cross_model_enabled=True, usage_provider_id="grok")
    env.requested_tool["name"] = tool_name
    req = await prepared(env)
    response = await env.provider.text_chat(prompt="usage", func_tool=req.func_tool)
    oauth = env.runtime.oauth
    await oauth.bind(replace(oauth.snapshot(), user_id="new-account"), expected_epoch=oauth.epoch)
    result = json.loads(
        await req.func_tool.get_tool(response.tools_call_name[0]).handler(env.event)
    )
    assert result["status"] == "denied" and "/v1/billing" not in env.calls


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
async def test_consumed_grok_proof_is_not_reauthorized_by_cross_model_switch(env, tool_name):
    env.runtime.config.update(usage_cross_model_enabled=True, usage_provider_id="grok")
    env.requested_tool["name"] = tool_name
    req = await prepared(env)
    response = await env.provider.text_chat(prompt="usage", func_tool=req.func_tool)
    name = response.tools_call_name[0]
    assert json.loads(await req.func_tool.get_tool(name).handler(env.event))["status"] == "success"
    assert json.loads(await req.func_tool.get_tool(name).handler(env.event))["status"] == "denied"


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
async def test_ordinary_user_can_prepare_and_call_grok_usage(env, tool_name):
    env.access["admin"] = False
    env.requested_tool["name"] = tool_name
    req = await prepared(env)
    assert req.func_tool.names() == list(USAGE_TOOL_NAMES)
    response = await env.provider.text_chat(prompt="usage", func_tool=req.func_tool)
    result = json.loads(
        await req.func_tool.get_tool(response.tools_call_name[0]).handler(env.event)
    )
    assert result["status"] == "success"


@pytest.mark.parametrize("tool_name", USAGE_TOOL_NAMES)
async def test_disabled_usage_tools_are_hidden_and_cannot_query(env, tool_name):
    env.runtime.config["usage_tools_enabled"] = False
    env.requested_tool["name"] = tool_name
    req = await prepared(env)
    assert not req.func_tool.tools
    for tool in env.service.build_tools():
        assert json.loads(await tool.handler(env.event))["status"] == "denied"
    assert "/v1/billing" not in env.calls


@pytest.mark.parametrize("command", ["usage", "usage_breakdown"])
@pytest.mark.parametrize(
    "admin,private,allow,allowed",
    [
        (False, True, False, False),
        (True, True, False, True),
        (True, False, False, False),
        (True, False, True, True),
    ],
)
async def test_direct_usage_commands_permissions(env, command, admin, private, allow, allowed):
    from astrbot_plugin_grok_oauth.main import GrokOAuthPlugin

    env.event.is_admin = lambda: admin
    env.event.is_private_chat = lambda: private
    env.event.plain_result = lambda text: text
    if allow:
        env.runtime.config["usage_group_allowlist"] = [env.event.unified_msg_origin]
    output = [
        value
        async for value in getattr(GrokOAuthPlugin, command)(
            SimpleNamespace(runtime=env.runtime), env.event
        )
    ]
    assert bool(env.calls.count("/v1/billing")) is allowed
    assert ("PermissionDenied" in output[0]) is (not allowed)
