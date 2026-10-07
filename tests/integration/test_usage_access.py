import json
from types import SimpleNamespace

import httpx
import pytest
from astrbot_plugin_grok_oauth.grok_oauth.models import TokenSnapshot
from test_plugin import plugin as plugin
from test_plugin import web_client as web_client


async def test_dashboard_usage_requires_authentication_before_service(plugin, web_client):
    inst, _, _ = plugin
    client, headers = web_client
    response = await client.get("/api/plug/grok-oauth/usage")
    assert response.status_code == 401
    response = await client.get("/api/plug/grok-oauth/usage", headers=headers("admin"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "unbound" and data["used_percent"] is None
    response = await client.get(
        "/api/plug/grok-oauth/usage?refresh=anything", headers=headers("admin")
    )
    assert response.status_code == 400


@pytest.mark.parametrize("admin,private", [(False, True), (True, False), (True, True)])
async def test_direct_usage_command_is_independent_of_llm(plugin, admin, private):
    inst, _, _ = plugin
    assert hasattr(inst, "usage"), "usage command is not implemented"
    event = SimpleNamespace(
        is_admin=lambda: admin,
        is_private_chat=lambda: private,
        plain_result=lambda text: text,
        unified_msg_origin="test:GroupMessage:1",
    )
    results = [value async for value in inst.usage(event)]
    if admin and private:
        assert "未绑定" in results[0]
    else:
        assert "PermissionDenied" in results[0]


@pytest.mark.parametrize("schema_mode", ["full", "skills_like"])
@pytest.mark.parametrize("child_is_grok", [False, True])
@pytest.mark.parametrize("cross_enabled", [False, True])
@pytest.mark.parametrize("prepared_tools", [False, True])
async def test_real_context_usage_child_agent_scope(
    plugin, monkeypatch, child_is_grok, cross_enabled, schema_mode, prepared_tools
):
    tool_name = "grok_usage_status"

    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.pipeline.process_stage.follow_up import (
        register_active_runner,
        unregister_active_runner,
    )
    from astrbot.core.platform.astr_message_event import AstrMessageEvent, MessageType
    from astrbot.core.provider.entities import (
        LLMResponse,
        ProviderMeta,
        ProviderRequest,
        ProviderType,
    )
    from astrbot_plugin_grok_oauth.astrbot_adapter.compat import (
        ProviderBase,
        ProviderOpenAIResponses,
    )

    inst, context, manager = plugin

    async def no_stats(*args, **kwargs):
        pass

    from astrbot.core.star import context as host_context

    if hasattr(host_context, "record_agent_runner_stats"):
        monkeypatch.setattr(host_context, "record_agent_runner_stats", no_stats)

    class Event(AstrMessageEvent):
        def get_sender_name(self):
            return "test"

        def get_message_outline(self):
            return "test"

        async def send(self, chain):
            self.sent.append(chain)

    event = Event(
        "test",
        SimpleNamespace(
            type=MessageType.GROUP_MESSAGE,
            message_id="child-message",
            message=[],
            sender=SimpleNamespace(user_id="1", nickname="test"),
        ),
        SimpleNamespace(id="test", name="test"),
        "child-scope",
    )
    event.sent = []
    event.is_admin = lambda: False
    event.is_private_chat = lambda: True
    for identifier in ("grok-parent", "grok-child"):
        config = {
            "id": identifier,
            "type": "grok_oauth_chat_completion",
            "model": "grok-4.6",
            "enable": True,
            "modalities": ["image", "audio", "tool_use"],
        }
        manager.providers_config.append(config)
        await manager.load_provider(config)
    parent = manager.inst_map["grok-parent"]
    args = {}

    class Foreign(ProviderOpenAIResponses):
        def __init__(self):
            ProviderBase.__init__(
                self, {"id": "foreign", "type": "test_foreign", "model": "foreign"}, {}
            )
            self.set_model("foreign")

        def meta(self):
            return ProviderMeta(
                id="foreign",
                model="foreign",
                type="test_foreign",
                provider_type=ProviderType.CHAT_COMPLETION,
            )

        def get_current_key(self):
            return "synthetic"

        async def terminate(self):
            pass

        async def text_chat(self, **kwargs):
            messages = self._ensure_message_to_dicts(kwargs.get("contexts", []))
            if any(item.get("role") == "tool" for item in messages):
                return LLMResponse("assistant", completion_text="done")
            return LLMResponse(
                "tool",
                tools_call_name=[tool_name],
                tools_call_args=[args.copy()],
                tools_call_ids=["foreign-call"],
            )

        async def text_chat_stream(self, **kwargs):
            yield await self.text_chat(**kwargs)

    foreign = Foreign()
    manager.inst_map["foreign"] = foreign
    if cross_enabled:
        inst.runtime.config.update(usage_cross_model_enabled=True, usage_provider_id="grok-parent")
    await inst.runtime.oauth.bind(
        TokenSnapshot(
            "default",
            "synthetic-access",
            "synthetic-refresh",
            None,
            "api:access",
            inst.runtime.client_id,
            user_id="synthetic-usage-user",
        ),
        expected_epoch=inst.runtime.oauth.epoch,
    )
    backend_calls = []

    def wire(request):
        if request.url.path == "/v1/billing":
            backend_calls.append("billing")
            return httpx.Response(200, json={"config": {"creditUsagePercent": 25}})
        payload = json.loads(request.content)
        native = any(tool.get("type") == "web_search" for tool in payload.get("tools", []))
        returned = any(item.get("type") == "function_call_output" for item in payload["input"])
        items = []
        if native:
            backend_calls.append("search")
            items.append({"type": "web_search_call", "id": "ws", "status": "completed"})
        if native or returned:
            items.append({"type": "message", "content": [{"type": "output_text", "text": "done"}]})
        else:
            items.append(
                {
                    "type": "function_call",
                    "call_id": "grok-call",
                    "name": tool_name,
                    "arguments": json.dumps(args),
                }
            )
        return httpx.Response(
            200, json={"id": "r", "model": "grok-4.6", "status": "completed", "output": items}
        )

    active = ToolLoopAgentRunner()
    await active.reset(
        provider=parent,
        request=ProviderRequest(prompt="parent"),
        run_context=ContextWrapper(context=SimpleNamespace(event=event)),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks(),
    )
    register_active_runner(event.unified_msg_origin, active)
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        original = inst.runtime.http._client
        inst.runtime.http._client = client
        original_billing = inst.runtime.billing._http
        inst.runtime.billing._http = client
        try:
            tool = next(t for t in manager.llm_tools.func_list if t.name == tool_name)
            child_request = ProviderRequest(prompt="child", func_tool=ToolSet([tool]))
            if prepared_tools:
                await inst.usage_tools.prepare(event, child_request)
            result = await context.tool_loop_agent(
                event=event,
                chat_provider_id="grok-child" if child_is_grok else "foreign",
                prompt="child",
                tools=child_request.func_tool,
                max_steps=3,
                agent_hooks=BaseAgentRunHooks(),
                tool_schema_mode=schema_mode,
            )
            assert result.completion_text == "done"
            assert len(backend_calls) == int(prepared_tools and child_is_grok)
        finally:
            inst.runtime.http._client = original
            inst.runtime.billing._http = original_billing
            unregister_active_runner(event.unified_msg_origin, active)
            manager.inst_map.pop("foreign", None)
