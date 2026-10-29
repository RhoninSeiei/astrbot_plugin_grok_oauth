"""Request snapshots retain the real host's permission checks and global tools."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot_plugin_grok_oauth.astrbot_adapter import compat
from astrbot_plugin_grok_oauth.astrbot_adapter.search_tools import SearchToolService
from astrbot_plugin_grok_oauth.astrbot_adapter.tool_scope import VIDEO_TOOL_NAMES
from astrbot_plugin_grok_oauth.astrbot_adapter.tools import TOOL_NAMES
from astrbot_plugin_grok_oauth.astrbot_adapter.video_tools import VideoToolService
from astrbot_plugin_grok_oauth.grok_oauth.errors import PermissionDenied
from astrbot_plugin_grok_oauth.main import GrokOAuthPlugin
from test_search_integration import search_env as search_env
from test_usage_tools import env as env


def ordinary_tool(name="other_plugin", *, active=True):
    return FunctionTool(
        name=name,
        description="other tool",
        parameters={"type": "object", "properties": {}},
        handler=AsyncMock(return_value="other-result"),
        active=active,
    )


def manager_with(tools, denied=False):
    manager = FunctionToolManager()
    manager.func_list.extend(tools)
    permission_mock = (
        AsyncMock if inspect.iscoroutinefunction(manager._check_tool_permission) else Mock
    )
    manager._check_tool_permission = permission_mock(
        return_value="host-permission-denied" if denied else None
    )
    return manager


def assert_permission_checked(manager):
    check = manager._check_tool_permission
    check.assert_called_once()
    if isinstance(check, AsyncMock):
        check.assert_awaited_once()


async def execute(tool, event, **arguments):
    context = ContextWrapper(context=SimpleNamespace(event=event), tool_call_timeout=5)
    results = [item async for item in FunctionToolExecutor.execute(tool, context, **arguments)]
    return "".join(
        part.text
        for result in results
        if result is not None
        for part in result.content
        if hasattr(part, "text")
    )


@pytest.mark.parametrize("request_kind", ["ordinary", "proactive"])
@pytest.mark.parametrize("container_kind", ["manager", "tool_set"])
@pytest.mark.parametrize("selection", ["empty", "other", "grok"])
async def test_hook_accepts_host_containers_without_mutating_global_tools(
    env, request_kind, container_kind, selection
):
    other, inactive = ordinary_tool(), ordinary_tool("inactive_other", active=False)
    tools = [] if selection == "empty" else [other, inactive]
    if selection == "grok":
        tools += SearchToolService(env.service.context, env.runtime).build_tools()
        tools += env.service.build_tools()
        tools += [ordinary_tool(name) for name in TOOL_NAMES + VIDEO_TOOL_NAMES]
    original = manager_with(tools) if container_kind == "manager" else ToolSet(tools)
    original_tools = list(tools)
    image_tools = SimpleNamespace(resolve_provider=AsyncMock(side_effect=PermissionDenied()))
    plugin = object.__new__(GrokOAuthPlugin)
    plugin.image_tools = image_tools
    plugin.search_tools = SearchToolService(env.service.context, env.runtime)
    plugin.video_tools = VideoToolService(env.service.context, env.runtime, image_tools)
    plugin.video_tools.resolve_provider = AsyncMock(side_effect=PermissionDenied())
    plugin.usage_tools = env.service
    req = (
        ProviderRequest(prompt="hello", func_tool=original)
        if request_kind == "ordinary"
        else SimpleNamespace(prompt="hello", func_tool=original, extra_user_content_parts=[])
    )
    await plugin.prepare_tools(env.event, req)
    assert isinstance(req.func_tool, ToolSet)
    assert (original.func_list if container_kind == "manager" else original.tools) == original_tools
    assert all(tool.active for tool in tools if tool is not inactive)
    if selection == "empty":
        assert req.func_tool.empty()
        image_tools.resolve_provider.assert_not_called()
    else:
        assert req.func_tool.get_tool("inactive_other").active is False
        selected = req.func_tool.get_tool("other_plugin")
        assert await execute(selected, env.event) == "other-result"
        if container_kind == "manager":
            assert_permission_checked(original)
        else:
            assert selected is other
    if selection == "grok":
        assert not set(TOOL_NAMES + VIDEO_TOOL_NAMES).intersection(req.func_tool.names())
        assert {"grok_web_search", "grok_usage_status", "grok_usage_breakdown"}.issubset(
            req.func_tool.names()
        )
    assert env.calls == []


@pytest.mark.parametrize("service_name", ["search", "usage", "video"])
@pytest.mark.parametrize("selection", ["empty", "other"])
async def test_independent_service_preparation_normalizes_manager(env, service_name, selection):
    image = SimpleNamespace()
    service = {
        "search": SearchToolService(env.service.context, env.runtime),
        "usage": env.service,
        "video": VideoToolService(env.service.context, env.runtime, image),
    }[service_name]
    manager = manager_with([] if selection == "empty" else [ordinary_tool()])
    req = SimpleNamespace(func_tool=manager)
    await service.prepare(env.event, req)
    assert isinstance(req.func_tool, ToolSet)
    assert req.func_tool.names() == ([] if selection == "empty" else ["other_plugin"])
    assert env.calls == []


@pytest.mark.parametrize("derived", [None, "get_light_tool_set", "get_param_only_tool_set"])
@pytest.mark.parametrize("denied", [False, True])
@pytest.mark.parametrize("tool_name", ["grok_usage_status", "grok_usage_breakdown"])
async def test_usage_binding_preserves_host_permissions_and_authorization(
    env, derived, denied, tool_name
):
    env.requested_tool["name"] = tool_name
    tools = env.service.build_tools()
    original_handlers = [tool.handler for tool in tools]
    manager = manager_with(tools, denied)
    req = SimpleNamespace(func_tool=manager)
    await env.service.prepare(env.event, req)
    selected = getattr(req.func_tool, derived)() if derived else req.func_tool
    response = await env.provider.text_chat(prompt="usage", func_tool=selected)
    tool = selected.get_tool(response.tools_call_name[0])
    output = await execute(tool, env.event)
    assert_permission_checked(manager)
    assert tool.handler is None
    assert [item.handler for item in tools] == original_handlers
    if denied:
        assert output == "host-permission-denied"
        assert "/v1/billing" not in env.calls
    else:
        assert '"status": "success"' in output
        assert env.calls.count("/v1/billing") == 1
        replay = await execute(selected.get_tool(str(response.tools_call_name[0])), env.event)
        assert '"status": "denied"' in replay
        assert env.calls.count("/v1/billing") == 1


@pytest.mark.parametrize("denied", [False, True])
async def test_search_request_binding_preserves_host_permissions(search_env, denied):
    env = search_env
    env.behavior["need_search"] = True
    original = env.service.build_tools()[0]
    manager = manager_with([original], denied)
    req = ProviderRequest(prompt="search", func_tool=manager)
    await env.service.prepare(env.event, req)
    response = await env.provider.text_chat(prompt="search", func_tool=req.func_tool)
    name, arguments = response.tools_call_name[0], response.tools_call_args[0]
    tool = req.func_tool.get_tool(name)
    assert tool.handler is None
    assert original.handler == env.service._tool_search
    output = await execute(tool, env.event, **arguments)
    assert_permission_checked(manager)
    assert len(env.calls) == (1 if denied else 2)
    assert (output == "host-permission-denied") if denied else ('"status": "success"' in output)


@pytest.mark.parametrize("container", [[], (), {}, object()])
def test_unknown_tool_container_is_rejected_explicitly(container):
    with pytest.raises(TypeError, match="tool container"):
        compat.request_tool_set(container)


async def test_other_plugin_tool_permission_remains_effective(env):
    original = ordinary_tool()
    manager = manager_with([original], denied=True)
    req = SimpleNamespace(func_tool=manager)
    await env.service.prepare(env.event, req)
    assert (
        await execute(req.func_tool.get_tool(original.name), env.event) == "host-permission-denied"
    )
    original.handler.assert_not_called()
    assert_permission_checked(manager)
    assert manager.func_list == [original]


@pytest.mark.parametrize("invalid_snapshot", [None, [], object()])
def test_invalid_manager_snapshot_fails_explicitly(invalid_snapshot):
    manager = manager_with([])
    manager.get_full_tool_set = lambda: invalid_snapshot
    with pytest.raises(TypeError, match="tool container"):
        compat.request_tool_set(manager)


def test_manager_snapshot_errors_are_preserved():
    manager = manager_with([])
    error = RuntimeError("host tool registry failure")

    def failed_snapshot():
        raise error

    manager.get_full_tool_set = failed_snapshot
    with pytest.raises(RuntimeError) as caught:
        compat.request_tool_set(manager)
    assert caught.value is error
