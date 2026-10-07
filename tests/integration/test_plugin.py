import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import astrbot_plugin_grok_oauth.main as plugin_module
import httpx
import jwt
import pytest
from astrbot.api.star import Context
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.provider.register import provider_cls_map
from astrbot.dashboard.api.plugins import legacy_router
from astrbot.dashboard.responses import ApiError
from astrbot_plugin_grok_oauth.astrbot_adapter.runtime import GrokRuntime
from astrbot_plugin_grok_oauth.grok_oauth.models import DeviceFlow, TokenSnapshot
from fastapi import FastAPI
from fastapi.responses import JSONResponse


@pytest.fixture
async def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    schema = json.loads((Path(plugin_module.__file__).parent / "_conf_schema.json").read_text())
    settings = AstrBotConfig(str(tmp_path / "plugin.json"), schema=schema)
    core = AstrBotConfig(
        str(tmp_path / "core.json"),
        default_config={
            "provider": [],
            "provider_sources": [],
            "provider_settings": {},
            "agent_runner": {"runner_type": "local", "config": {}},
        },
    )
    acm = SimpleNamespace(confs={"default": core}, default_conf=core)
    persona = SimpleNamespace(default_persona="default")
    manager = ProviderManager(acm, None, persona)
    manager.llm_tools = FunctionToolManager()

    async def cid(umo):
        return "conversation-test"

    context = Context(
        event_queue=asyncio.Queue(),
        config=core,
        db=None,
        provider_manager=manager,
        platform_manager=SimpleNamespace(platform_insts=[]),
        conversation_manager=SimpleNamespace(get_curr_conversation_id=cid),
        message_history_manager=None,
        persona_manager=persona,
        astrbot_config_mgr=acm,
        knowledge_base_manager=None,
        cron_manager=None,
    )
    context.registered_web_apis = []

    def build_runtime(config, root, **kwargs):
        return GrokRuntime(
            config,
            root,
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": []})),
            **kwargs,
        )

    monkeypatch.setattr(plugin_module, "GrokRuntime", build_runtime)
    inst = plugin_module.GrokOAuthPlugin(context, settings)
    await inst.initialize()
    yield inst, context, manager
    await inst.terminate()


async def test_plugin_initialization_registers_templates_and_only_owned_tools(plugin):
    inst, context, manager = plugin
    meta = provider_cls_map["grok_oauth_chat_completion"]
    assert meta.provider_display_name == "Grok Oauth"
    assert {tool.name for tool in manager.llm_tools.func_list} == {
        "grok_image_generate",
        "grok_image_edit",
        "grok_web_search",
        "grok_usage_status",
        "grok_usage_breakdown",
        "grok_video_generate",
        "grok_video_edit",
        "grok_video_status",
    }
    assert len(context.registered_web_apis) == 18
    assert inst.runtime.oauth.snapshot() is None
    assert all(
        tool.parameters["additionalProperties"] is False for tool in manager.llm_tools.func_list
    )


async def test_core_loads_saved_model_and_reload_rebuilds_only_target(plugin):
    inst, context, manager = plugin
    config = {
        "id": "grok-one",
        "type": "grok_oauth_chat_completion",
        "provider_type": "chat_completion",
        "model": "grok-4.6",
        "enable": True,
    }
    manager.providers_config.append(config)
    await manager.load_provider(config)
    original = manager.inst_map["grok-one"]
    unrelated = SimpleNamespace(name="unrelated")
    manager.inst_map["untouched"] = unrelated
    manager.curr_provider_inst = unrelated
    await inst.terminate()
    assert original._closed and manager.inst_map == {"untouched": unrelated}
    assert manager.curr_provider_inst is unrelated
    newer = plugin_module.GrokOAuthPlugin(context, inst.config)
    try:
        await newer.initialize()
        assert manager.inst_map["grok-one"] is not original
        assert manager.inst_map["grok-one"]._runtime is newer.runtime
        assert manager.curr_provider_inst is unrelated
        assert (
            len([p for p in manager.provider_insts if p.provider_config["id"] == "grok-one"]) == 1
        )
    finally:
        await newer.terminate()


async def test_cold_open_restores_credentials_without_network_probe(plugin):
    inst, context, manager = plugin
    token = TokenSnapshot(
        "default",
        "cold-test-access",
        "cold-test-refresh",
        None,
        "api:access",
        inst.runtime.client_id,
    )
    await inst.runtime.oauth.bind(token, expected_epoch=inst.runtime.oauth.epoch)
    await inst.terminate()
    newer = plugin_module.GrokOAuthPlugin(context, inst.config)
    try:
        await newer.initialize()
        assert newer.runtime.oauth.snapshot().access_token == "cold-test-access"
    finally:
        await newer.terminate()


@pytest.fixture
async def web_client(plugin):
    inst, context, manager = plugin
    app = FastAPI()
    app.state.jwt_secret = "synthetic-test-signing-key-at-least-32-bytes"
    app.state.core_lifecycle = SimpleNamespace(star_context=context)

    @app.exception_handler(ApiError)
    async def api_error(request, error):
        return JSONResponse({"error": error.message}, status_code=error.status_code)

    app.include_router(legacy_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        def headers(user):
            token = jwt.encode(
                {"username": user, "exp": time.time() + 60}, app.state.jwt_secret, algorithm="HS256"
            )
            return {"Authorization": "Bearer " + token}

        yield client, headers


@pytest.mark.parametrize(
    "method,path", [("POST", "auth/start"), ("GET", "auth/status"), ("POST", "auth/disconnect")]
)
async def test_actual_dashboard_route_rejects_unauthenticated(web_client, method, path):
    client, headers = web_client
    result = await client.request(method, "/api/plug/grok-oauth/" + path, json={})
    assert result.status_code == 401


@pytest.mark.parametrize("api_prefix", ["grok-oauth", "astrbot_plugin_grok_oauth"])
async def test_actual_dashboard_route_enforces_owner_and_excludes_credentials(
    plugin, web_client, api_prefix
):
    inst, context, manager = plugin
    client, headers = web_client
    gate = asyncio.Event()

    async def start(*, owner_id, epoch):
        return DeviceFlow(
            "flow-one",
            owner_id,
            "SAFE-CODE",
            "https://auth.x.ai/device",
            "secret-device-value",
            60,
            1,
            epoch,
            time.monotonic() + 60,
            time.time() + 60,
        )

    async def poll(flow):
        await gate.wait()
        return TokenSnapshot(
            "default",
            "secret-access-value",
            "secret-refresh-value",
            None,
            "api:access",
            inst.runtime.client_id,
        )

    inst.runtime.wire.start_device_flow = start
    inst.runtime.wire.poll_device_flow = poll
    body = {
        "confirmed_client_id": inst.runtime.client_id,
        "client_profile": inst.runtime.client_profile,
    }
    response = await client.post(
        f"/api/plug/{api_prefix}/auth/start", headers=headers("admin-a"), json=body
    )
    assert response.status_code == 200
    assert "secret-device-value" not in response.text
    foreign = await client.get(
        f"/api/plug/{api_prefix}/auth/status?flow_id=flow-one", headers=headers("admin-b")
    )
    assert foreign.status_code == 403
    foreign_cancel = await client.post(
        f"/api/plug/{api_prefix}/auth/cancel",
        headers=headers("admin-b"),
        json={"flow_id": "flow-one"},
    )
    assert foreign_cancel.status_code == 403
    gate.set()
    await inst.runtime.wait_for_flow()
    status = await client.get(
        f"/api/plug/{api_prefix}/auth/status?flow_id=flow-one", headers=headers("admin-a")
    )
    assert status.json()["data"]["status"] == "authorized"
    assert all(
        secret not in status.text
        for secret in ["secret-access-value", "secret-refresh-value", "secret-device-value"]
    )


async def test_admin_command_body_rejects_group_and_nonadmin(plugin):
    inst, context, manager = plugin
    for admin, private in [(False, True), (True, False)]:
        event = SimpleNamespace(
            is_admin=lambda: admin, is_private_chat=lambda: private, plain_result=lambda text: text
        )
        replies = [value async for value in inst.login(event, "confirm")]
        assert replies == ["Grok OAuth：PermissionDenied"]
    assert inst.runtime.oauth.snapshot() is None


async def test_live_probe_needs_explicit_run(plugin, web_client):
    inst, context, manager = plugin
    config = {
        "id": "grok-one",
        "type": "grok_oauth_chat_completion",
        "provider_type": "chat_completion",
        "model": "grok-4.6",
        "enable": True,
    }
    await manager.load_provider(config)
    client, headers = web_client
    response = await client.post(
        "/api/plug/grok-oauth/test/image",
        headers=headers("admin"),
        json={"provider_id": "grok-one"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("repair_source_link", [False, True])
async def test_source_rename_delete_recreate_with_preexisting_model(
    plugin, monkeypatch, repair_source_link
):
    """A preinstalled model must belong to its Dashboard source to follow deletion."""
    import astrbot.core.provider.manager as manager_module
    from astrbot.dashboard.services.config_service import ProviderConfigService

    inst, context, manager = plugin
    core = manager.acm.default_conf
    monkeypatch.setattr(manager_module, "astrbot_config", core)
    service = ProviderConfigService(SimpleNamespace(astrbot_config=core, provider_manager=manager))
    source = {
        "id": "grok_oauth",
        "type": "grok_oauth_chat_completion",
        "provider_type": "chat_completion",
        "enable": True,
    }
    core["provider_sources"].append(source)
    model = {
        "id": "grok_oauth/grok-4.6",
        "type": "grok_oauth_chat_completion",
        "provider_type": "chat_completion",
        "model": "grok-4.6",
        "enable": True,
    }
    await manager.create_provider(dict(model))
    original = manager.inst_map[model["id"]]
    if repair_source_link:
        model["provider_source_id"] = source["id"]
        await manager.update_provider(model["id"], dict(model))
        assert original._closed
        assert len(service.list_providers(source_id=source["id"])["providers"]) == 1
    else:
        assert service.list_providers(source_id=source["id"])["providers"] == []

    await service.upsert_provider_source(source["id"], {**source, "id": "renamed-grok"})
    await service.delete_provider_source("renamed-grok")
    assert not inst.runtime.closed
    if not repair_source_link:
        assert model["id"] in manager.inst_map
        with pytest.raises(ValueError, match="Provider ID grok_oauth/grok-4.6 already exists"):
            await manager.create_provider({**model, "provider_source_id": source["id"]})
        return

    assert model["id"] not in manager.inst_map
    assert not any(p["id"] == model["id"] for p in core["provider"])
    await service.upsert_provider_source(source["id"], source)
    await service.create_provider(dict(model), source_id=source["id"])
    recreated = manager.inst_map[model["id"]]
    assert recreated._runtime is inst.runtime and not recreated._closed
    assert len(service.list_providers(source_id=source["id"])["providers"]) == 1
    assert len([p for p in manager.provider_insts if p.provider_config["id"] == model["id"]]) == 1


async def test_dashboard_source_template_uses_public_name(plugin):
    from astrbot.dashboard.services.config_service import ProviderConfigService

    _, _, manager = plugin
    service = ProviderConfigService(
        SimpleNamespace(astrbot_config=manager.acm.default_conf, provider_manager=manager)
    )
    templates = service.get_provider_schema()["config_schema"]["provider"]["config_template"]
    grok = {
        name: tmpl
        for name, tmpl in templates.items()
        if tmpl.get("type") == "grok_oauth_chat_completion"
    }
    assert list(grok) == ["Grok Oauth"]
    assert grok["Grok Oauth"]["id"] == "grok_oauth"
    assert not {"key", "grok_account_slot"} & grok["Grok Oauth"].keys()
    manager.acm.default_conf["provider_sources"].append(dict(grok["Grok Oauth"]))
    catalog = await service.list_provider_source_models("grok_oauth")
    assert "grok-4.6" in catalog["models"]


@pytest.mark.parametrize("existing_sources", [0, 1, 2])
async def test_initialize_migrates_legacy_models_without_fixed_ids(plugin, existing_sources):
    inst, context, manager = plugin
    await inst.terminate()
    core = manager.acm.default_conf
    sources = [
        {"id": f"custom-source-{i}", "type": "grok_oauth_chat_completion", "enable": True}
        for i in range(existing_sources)
    ]
    sources.append({"id": "grok_oauth", "type": "unrelated_provider"})
    core["provider_sources"] = sources
    core["provider"] = [
        {"id": "arbitrary-existing-model", "type": "grok_oauth_chat_completion", "enable": False},
        {"id": "another-model", "type": "grok_oauth_chat_completion", "enable": False},
        {"id": "leave-me", "type": "unrelated_provider", "enable": False},
    ]
    newer = plugin_module.GrokOAuthPlugin(context, inst.config)
    try:
        await newer.initialize()
        models = core["provider"]
        assert [p["id"] for p in models] == [
            "arbitrary-existing-model",
            "another-model",
            "leave-me",
        ]
        assert "provider_source_id" not in models[2]
        linked = {p["provider_source_id"] for p in models[:2]}
        assert len(linked) == 1
        target_id = linked.pop()
        if existing_sources == 1:
            assert target_id == "custom-source-0"
        else:
            assert target_id not in {p["id"] for p in sources}
        assert (
            next(s for s in core["provider_sources"] if s["id"] == target_id)["type"]
            == "grok_oauth_chat_completion"
        )
        assert core["provider_sources"][: len(sources)] == sources
        assert manager.providers_config is core["provider"]
        snapshot = (
            json.loads(
                await asyncio.to_thread(Path(core.config_path).read_text, encoding="utf-8-sig")
            )
            if hasattr(core, "config_path")
            else dict(core)
        )
        await newer.terminate()
        newest = plugin_module.GrokOAuthPlugin(context, inst.config)
        try:
            await newest.initialize()
            assert dict(core) == snapshot
        finally:
            await newest.terminate()
    finally:
        await newer.terminate()


async def test_plugin_page_auth_route_needs_no_model(web_client):
    client, headers = web_client
    response = await client.get(
        "/api/plug/astrbot_plugin_grok_oauth/auth/client", headers=headers("admin")
    )
    assert response.status_code == 200
    assert response.json()["data"]["client_id"]


async def test_source_migration_save_failure_restores_memory_and_registration(plugin, monkeypatch):
    inst, context, manager = plugin
    await inst.terminate()
    core = manager.acm.default_conf
    original = [{"id": "legacy", "type": "grok_oauth_chat_completion", "enable": False}]
    core["provider"] = original
    sources = core["provider_sources"]

    original_write = type(core)._write_config_snapshot

    def fail_after_snapshot(config, *args, **kwargs):
        if config is core:
            raise OSError("synthetic storage failure")
        return original_write(config, *args, **kwargs)

    monkeypatch.setattr(type(core), "_write_config_snapshot", fail_after_snapshot)
    newer = plugin_module.GrokOAuthPlugin(context, inst.config)
    with pytest.raises(OSError, match="synthetic storage failure"):
        await newer.initialize()
    assert core["provider"] is original and core["provider_sources"] is sources
    assert "grok_oauth_chat_completion" not in provider_cls_map
    assert not context.registered_web_apis
    assert not [
        t
        for t in manager.llm_tools.func_list
        if t.name in {"grok_image_generate", "grok_image_edit", "grok_web_search"}
    ]


@pytest.mark.parametrize("tool_name", ["grok_web_search", "grok_image_generate", "grok_image_edit"])
@pytest.mark.parametrize("schema_mode", ["full", "skills_like"])
@pytest.mark.parametrize("child_is_grok", [False, True])
@pytest.mark.parametrize("cross_enabled", [False, True])
async def test_real_context_child_agent_tool_scope(
    plugin, monkeypatch, tool_name, child_is_grok, cross_enabled, schema_mode
):
    import base64
    from io import BytesIO

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
    from PIL import Image as PILImage

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
    args = {"query": "facts"} if tool_name == "grok_web_search" else {"prompt": "draw red box"}

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
        inst.runtime.config["search_provider_id"] = "grok-parent"
        inst.runtime.config["tools_provider_id"] = "grok-parent"
    await inst.runtime.oauth.bind(
        TokenSnapshot(
            "default",
            "synthetic-access",
            "synthetic-refresh",
            None,
            "api:access",
            inst.runtime.client_id,
        ),
        expected_epoch=inst.runtime.oauth.epoch,
    )
    data = BytesIO()
    PILImage.new("RGB", (16, 8), "red").save(data, format="PNG")
    if tool_name == "grok_image_edit":
        scope = await inst.image_tools.scope(event)
        asset_id = await inst.runtime.assets.import_reference(
            "data:image/png;base64," + base64.b64encode(data.getvalue()).decode(), scope=scope
        )
        args["reference_asset_ids"] = [asset_id]
    backend_calls = []

    def wire(request):
        payload = json.loads(request.content)
        if request.url.path.startswith("/v1/images/"):
            backend_calls.append("image")
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(data.getvalue()).decode()}]}
            )
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
        try:
            tool = next(t for t in manager.llm_tools.func_list if t.name == tool_name)
            result = await context.tool_loop_agent(
                event=event,
                chat_provider_id="grok-child" if child_is_grok else "foreign",
                prompt="child",
                tools=ToolSet([tool]),
                max_steps=3,
                agent_hooks=BaseAgentRunHooks(),
                tool_schema_mode=schema_mode,
            )
            assert result.completion_text == "done"
            assert len(backend_calls) == int(child_is_grok or cross_enabled)
        finally:
            inst.runtime.http._client = original
            unregister_active_runner(event.unified_msg_origin, active)
            manager.inst_map.pop("foreign", None)


async def test_native_model_migration_preserves_effective_source_values_and_core_edit(
    plugin, monkeypatch
):
    from copy import deepcopy

    from astrbot.dashboard.services.config_service import ProviderConfigService
    from astrbot_plugin_grok_oauth.astrbot_adapter.compat import migrate_legacy_provider_sources

    inst, context, manager = plugin
    core = manager.acm.default_conf
    source = {
        "id": "source",
        "type": "grok_oauth_chat_completion",
        "provider_type": "chat_completion",
        "enable": True,
        "api_base": "https://api.x.ai/v1",
        "timeout": 180,
        "key": ["oauth-managed"],
        "modalities": ["text"],
        "max_context_tokens": 23456,
        "custom_extra_body": {"temperature": 0.3},
    }
    old = {
        "id": "stable-old-name",
        "provider_source_id": "source",
        "model": "grok-4.7",
        "enable": True,
        "type": source["type"],
        "provider_type": "chat_completion",
        "key": ["oauth-managed"],
        "api_base": source["api_base"],
        "timeout": 180,
    }
    core["provider_sources"] = [source]
    core["provider"] = [old]
    manager.provider_sources_config = core["provider_sources"]
    manager.providers_config = core["provider"]
    import astrbot.core.provider.manager as manager_module

    monkeypatch.setattr(manager_module, "astrbot_config", core)
    before = manager.get_merged_provider_config(old)
    assert await migrate_legacy_provider_sources(context) == 1
    new = core["provider"][0]
    assert {"type", "key", "api_base", "provider_type", "timeout"}.isdisjoint(new)
    assert manager.get_merged_provider_config(new) == before
    assert core["provider_sources"] == [source]
    assert await migrate_legacy_provider_sources(context) == 0
    service = ProviderConfigService(SimpleNamespace(astrbot_config=core, provider_manager=manager))
    edited = deepcopy(new)
    edited.update(
        modalities=["text", "image", "tool_use"],
        max_context_tokens=45678,
        custom_extra_body={"max_tokens": 123, "reasoning_effort": "low"},
    )
    await service.update_provider(edited["id"], edited)
    assert core["provider"][0] == edited
    active = manager.inst_map[edited["id"]]
    assert active.provider_config["max_context_tokens"] == 45678
    payload = await active._payload(prompt="test")
    assert payload["max_output_tokens"] == 123 and payload["reasoning"] == {"effort": "low"}
