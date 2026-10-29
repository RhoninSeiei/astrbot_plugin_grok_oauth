import asyncio
import base64
import json
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Image
from astrbot_plugin_grok_oauth.astrbot_adapter.media_delivery import MediaDelivery
from astrbot_plugin_grok_oauth.astrbot_adapter.provider import GrokOAuthProvider
from astrbot_plugin_grok_oauth.astrbot_adapter.registration import (
    register_provider,
    unregister_provider,
)
from astrbot_plugin_grok_oauth.astrbot_adapter.registry_state import bind_runtime, clear_runtime
from astrbot_plugin_grok_oauth.astrbot_adapter.runtime import GrokRuntime
from astrbot_plugin_grok_oauth.astrbot_adapter.tools import ImageToolService
from astrbot_plugin_grok_oauth.grok_oauth.models import TokenSnapshot
from PIL import Image as PILImage


class Event:
    def __init__(self, umo="test:GroupMessage:1", cid="c1"):
        self.unified_msg_origin = umo
        self.cid = cid
        self.message_obj = SimpleNamespace(message_id="m1", message=[])
        self.sent = []
        self.extra = {}
        self.fail = False

    def get_platform_id(self):
        return "test"

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_result(self):
        return None

    async def send(self, chain):
        if self.fail:
            return False
        self.sent.append(chain)


@pytest.fixture
async def tool_environment(tmp_path):
    buf = BytesIO()
    PILImage.new("RGB", (16, 8), "red").save(buf, format="PNG")
    calls = []

    def wire(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}]}
        )

    runtime = GrokRuntime({}, tmp_path, transport=httpx.MockTransport(wire))
    await runtime.open()
    await runtime.oauth.bind(
        TokenSnapshot(
            "default", "test-access", "test-refresh", None, "api:access", runtime.client_id
        ),
        expected_epoch=runtime.oauth.epoch,
    )
    bind_runtime(runtime)
    registration = register_provider(runtime.owner_id, GrokOAuthProvider)
    provider = GrokOAuthProvider(
        {"id": "grok", "type": "grok_oauth_chat_completion", "model": "grok-4.6"}, {}
    )

    async def current(umo):
        return provider

    async def cid(umo):
        return "c1" if umo.endswith(":1") else "other"

    context = SimpleNamespace(
        get_using_provider_async=current,
        get_provider_by_id=lambda id: provider if id == "grok" else None,
        conversation_manager=SimpleNamespace(get_curr_conversation_id=cid),
    )
    delivery = MediaDelivery(runtime.data_root / "outbox.json", runtime.assets)
    service = ImageToolService(context, runtime, delivery)
    yield service, runtime, calls
    await service.close()
    await runtime.close()
    unregister_provider(registration)
    clear_runtime(runtime)


async def test_generate_sends_actual_attachment_and_edit_reuses_scope(tool_environment):
    service, runtime, calls = tool_environment
    event = Event()
    first = json.loads(await service.generate(event, prompt="draw a cat"))
    assert first["status"] == "sent" and len(event.sent) == 1
    assert isinstance(event.sent[0].chain[0], Image)
    assert "file://" in event.sent[0].chain[0].file
    assert "base64" not in json.dumps(first) and "/tmp/" not in json.dumps(first)
    event.message_obj.message_id = "m2"
    second = json.loads(
        await service.edit(event, prompt="blue cat", reference_asset_ids=first["asset_ids"])
    )
    assert second["status"] == "sent" and len(event.sent) == 2
    assert "image" in calls[-1]
    foreign = json.loads(
        await service.edit(
            Event(umo="test:GroupMessage:2"), prompt="steal", reference_asset_ids=first["asset_ids"]
        )
    )
    assert foreign["error"] == "AssetNotFound" and len(calls) == 2


async def test_failed_send_retry_only_sends_existing_asset(tool_environment):
    service, runtime, calls = tool_environment
    event = Event()
    event.fail = True
    first = json.loads(await service.generate(event, prompt="draw"))
    assert first["status"] == "failed"
    event.fail = False
    second = json.loads(await service.generate(event, prompt="draw"))
    assert second["status"] == "sent" and first["asset_ids"] == second["asset_ids"]
    assert len(calls) == 1 and len(event.sent) == 1
    again = json.loads(await service.generate(event, prompt="draw"))
    assert again["status"] == "sent" and len(event.sent) == 1


async def test_concurrent_same_operation_generates_once(tool_environment):
    service, runtime, calls = tool_environment
    event = Event()
    results = await asyncio.gather(*(service.generate(event, prompt="one") for _ in range(10)))
    assert len(calls) == 1 and len(event.sent) == 1
    assert len({json.loads(r)["operation_id"] for r in results}) == 1


async def test_tool_arguments_cannot_select_paths_or_providers(tool_environment):
    service, runtime, calls = tool_environment
    for kwargs in [
        {"provider_id": "other"},
        {"output_dir": "/etc"},
        {"reference_asset_ids": ["/etc/passwd"]},
    ]:
        result = json.loads(await service.generate(Event(), prompt="draw", **kwargs))
        assert result.get("error") in {"InvalidRequest", "AssetNotFound"}
    assert calls == []


async def test_real_executor_requires_model_call_or_explicit_backend(tool_environment):
    service, runtime, calls = tool_environment
    event = Event()
    tool = service.build_tools()[0]
    context = SimpleNamespace(event=event)
    wrapper = ContextWrapper(context=context, tool_call_timeout=10)
    outputs = [
        r async for r in FunctionToolExecutor.execute(tool, wrapper, prompt="executor image")
    ]
    summaries = [
        json.loads(part.text)
        for result in outputs
        if result
        for part in result.content
        if part.type == "text"
    ]
    assert summaries[0]["error"] == "PermissionDenied" and not event.sent and not calls
    runtime.config["tools_provider_id"] = "grok"
    outputs = [
        r async for r in FunctionToolExecutor.execute(tool, wrapper, prompt="executor image")
    ]
    summaries = [
        json.loads(part.text)
        for result in outputs
        if result
        for part in result.content
        if part.type == "text"
    ]
    assert summaries[0]["status"] == "sent" and len(event.sent) == 1 and len(calls) == 1


async def test_effective_defaults_share_one_generation(tool_environment):
    service, runtime, calls = tool_environment
    event = Event()
    first = json.loads(await service.generate(event, prompt="same"))
    second = json.loads(await service.generate(event, prompt="same", n=1))
    assert first["operation_id"] == second["operation_id"] and len(calls) == 1


async def test_outbox_reopen_sent_does_not_send_or_generate(tool_environment):
    service, runtime, calls = tool_environment
    event = Event()
    await service.generate(event, prompt="persist")
    key = next(iter(service.delivery.records))
    await service.delivery.close()
    reopened = MediaDelivery(service.delivery.path, runtime.assets)

    async def forbidden():
        raise AssertionError("must not generate")

    try:
        result = await reopened.run(
            key, scope=await service.scope(event), event=event, generate=forbidden
        )
        assert result["status"] == "sent" and len(event.sent) == 1 and len(calls) == 1
    finally:
        await reopened.close()


@pytest.mark.parametrize("state", ["pending", "sending"])
async def test_interrupted_outbox_reopens_unknown_without_side_effects(tool_environment, state):
    service, runtime, calls = tool_environment
    event = Event()
    await service.generate(event, prompt="persist")
    key = next(iter(service.delivery.records))
    await service.delivery._update(key, status=state)
    await service.delivery.close()
    reopened = MediaDelivery(service.delivery.path, runtime.assets)

    async def forbidden():
        raise AssertionError("must not generate")

    try:
        result = await reopened.run(
            key, scope=await service.scope(event), event=event, generate=forbidden
        )
        assert result["status"] == "unknown" and len(event.sent) == 1 and len(calls) == 1
    finally:
        await reopened.close()


async def test_receipt_loss_requires_explicit_resend_of_same_assets(tool_environment):
    service, runtime, calls = tool_environment
    event = Event()

    async def lost(chain):
        raise OSError("synthetic receipt lost")

    original = event.send
    event.send = lost
    first = json.loads(await service.generate(event, prompt="receipt"))
    assert first["status"] == "unknown"
    event.send = original
    repeated = json.loads(await service.generate(event, prompt="receipt"))
    assert repeated["status"] == "unknown" and not event.sent
    sent = await service.delivery.resend(
        first["operation_id"], scope=await service.scope(event), event=event
    )
    assert sent["status"] == "sent" and sent["asset_ids"] == first["asset_ids"] and len(calls) == 1


async def test_queued_resend_is_cancelled_before_close_returns(tool_environment):
    service, runtime, calls = tool_environment
    event = Event()
    first = json.loads(await service.generate(event, prompt="close"))
    scope = await service.scope(event)
    await service.delivery._lock.acquire()
    task = asyncio.create_task(
        service.delivery.resend(first["operation_id"], scope=scope, event=event)
    )
    await asyncio.sleep(0)
    try:
        await service.delivery.close()
        assert task.cancelled()
    finally:
        service.delivery._lock.release()
        await asyncio.gather(task, return_exceptions=True)
    assert len(event.sent) == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_full_real_agent_loop_generates_edits_and_finishes(tool_environment, streaming):
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.provider.entities import ProviderRequest

    service, runtime, image_calls = tool_environment
    event = Event()
    provider = await service.resolve_provider(event)
    base_transport = runtime.client._transport
    chat_requests = []

    async def handler(request):
        if request.url.path.startswith("/v1/images/"):
            return await base_transport.handle_async_request(request)
        payload = json.loads(request.content)
        chat_requests.append(payload)
        if len(chat_requests) == 1:
            output = [
                {
                    "type": "function_call",
                    "call_id": "gen-call",
                    "name": "grok_image_generate",
                    "arguments": json.dumps({"prompt": "first image"}),
                }
            ]
        elif len(chat_requests) == 2:
            returned = [
                item for item in payload["input"] if item.get("type") == "function_call_output"
            ]
            asset_ids = json.loads(returned[-1]["output"])["asset_ids"]
            output = [
                {
                    "type": "function_call",
                    "call_id": "edit-call",
                    "name": "grok_image_edit",
                    "arguments": json.dumps(
                        {"prompt": "blue version", "reference_asset_ids": asset_ids}
                    ),
                }
            ]
        else:
            output = [{"type": "message", "content": [{"type": "output_text", "text": "finished"}]}]
        response = {
            "id": "agent-" + str(len(chat_requests)),
            "model": "grok-4.6",
            "status": "completed",
            "output": output,
        }
        if payload.get("stream"):
            return httpx.Response(
                200,
                content=(
                    "data: "
                    + json.dumps({"type": "response.completed", "response": response})
                    + "\n\n"
                ).encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        original = runtime.http._client
        runtime.http._client = client
        try:
            req = ProviderRequest(
                prompt="Create then edit an image.", func_tool=ToolSet(service.build_tools())
            )
            runner = ToolLoopAgentRunner()
            wrapper = ContextWrapper(context=SimpleNamespace(event=event), tool_call_timeout=10)
            await runner.reset(
                provider=provider,
                request=req,
                run_context=wrapper,
                tool_executor=FunctionToolExecutor(),
                agent_hooks=BaseAgentRunHooks(),
                streaming=streaming,
            )
            [item async for item in runner.step_until_done(max_step=5)]
            assert runner.get_final_llm_resp().completion_text == "finished"
            assert len(chat_requests) == 3 and len(image_calls) == 2 and len(event.sent) == 2
            final_history = json.dumps([message.model_dump() for message in wrapper.messages])
            assert "asset_ids" in final_history and "b64_json" not in final_history
            assert runtime.data_root.as_posix() not in final_history
        finally:
            runtime.http._client = original


@pytest.mark.parametrize("persistent", [False, True])
async def test_generated_outbox_write_failure_retains_assets(
    tool_environment, monkeypatch, persistent
):
    service, runtime, calls = tool_environment
    event = Event()
    original = service.delivery._commit
    failed = False

    async def fail_result(records):
        nonlocal failed
        states = {record["status"] for record in records.values()}
        if "generated" in states and not failed:
            failed = True
            from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError

            raise ProtocolError("synthetic result disk failure")
        if failed and persistent:
            from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError

            raise ProtocolError("synthetic continuing disk failure")
        return await original(records)

    monkeypatch.setattr(service.delivery, "_commit", fail_result)
    result = json.loads(await service.generate(event, prompt="retain result"))
    assert result["status"] == "unknown" and result["error"] == "OutboxPersistenceError"
    assert len(result["asset_ids"]) == 1 and not event.sent
    repeated = json.loads(await service.generate(event, prompt="retain result"))
    assert repeated["asset_ids"] == result["asset_ids"] and len(calls) == 1
    monkeypatch.setattr(service.delivery, "_commit", original)
    resent = await service.delivery.resend(
        result["operation_id"], scope=await service.scope(event), event=event
    )
    assert resent["status"] == "sent" and len(event.sent) == 1 and len(calls) == 1


@pytest.mark.parametrize("selected_is_grok", [False, True])
async def test_image_tools_follow_explicit_request_provider(tool_environment, selected_is_grok):
    from astrbot_plugin_grok_oauth.grok_oauth.errors import PermissionDenied

    service, runtime, calls = tool_environment
    event = Event()
    grok = await service.resolve_provider(event)
    foreign = SimpleNamespace(provider_config={"id": "other-provider"})
    service.context.get_provider_by_id = lambda id: grok if id == "grok" else foreign

    async def default(umo):
        return foreign if selected_is_grok else grok

    service.context.get_using_provider_async = default
    event.set_extra("selected_provider", "grok" if selected_is_grok else "other-provider")
    if selected_is_grok:
        assert await service.resolve_provider(event) is grok
    else:
        with pytest.raises(PermissionDenied):
            await service.resolve_provider(event)
    assert calls == []


@pytest.mark.parametrize("cross_model_enabled", [False, True])
async def test_image_tool_checks_actual_fallback_model(tool_environment, cross_model_enabled):
    from astrbot.core.pipeline.process_stage.follow_up import (
        register_active_runner,
        unregister_active_runner,
    )
    from astrbot_plugin_grok_oauth.grok_oauth.errors import PermissionDenied

    service, runtime, calls = tool_environment
    event = Event()
    grok = await service.resolve_provider(event)
    if cross_model_enabled:
        runtime.config["tools_provider_id"] = "grok"
    runner = SimpleNamespace(
        run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
        provider=SimpleNamespace(provider_config={"id": "foreign"}),
        request_stop=lambda: None,
    )
    register_active_runner(event.unified_msg_origin, runner)
    try:
        if cross_model_enabled:
            assert await service.resolve_provider(event) is grok
        else:
            with pytest.raises(PermissionDenied):
                await service.resolve_provider(event)
        assert calls == []
    finally:
        unregister_active_runner(event.unified_msg_origin, runner)
