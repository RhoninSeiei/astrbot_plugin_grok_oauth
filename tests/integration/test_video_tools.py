"""Caller-owned video behavior with real AstrBot tools and components."""

import asyncio
import json
from dataclasses import asdict, dataclass
from types import SimpleNamespace

import pytest
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Image
from astrbot_plugin_grok_oauth.astrbot_adapter.compat import ToolSet
from astrbot_plugin_grok_oauth.astrbot_adapter.provider import GrokOAuthProvider
from astrbot_plugin_grok_oauth.astrbot_adapter.tool_scope import issue_tool_call
from astrbot_plugin_grok_oauth.astrbot_adapter.tools import ImageToolService
from astrbot_plugin_grok_oauth.astrbot_adapter.video_tools import VideoToolService
from astrbot_plugin_grok_oauth.grok_oauth.errors import (
    AssetNotFound,
    AuthorizationChanged,
    InvalidRequest,
)


class Event:
    def __init__(self, umo="test:GroupMessage:1", message_id="m1"):
        self.unified_msg_origin = umo
        self.message_obj = SimpleNamespace(message_id=message_id, message=[])
        self.extra = {}
        self.sends = []

    async def send(self, chain):
        self.sends.append(chain)
        raise AssertionError("Video capability tool must never send an attachment")

    def get_platform_id(self):
        return "test"

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_result(self):
        return None


@dataclass
class Job:
    job_id: str
    status: str = "pending"
    asset_id: str = ""
    model: str = "grok-imagine-video-1.5"
    duration: int = 6

    def public(self):
        return asdict(self)


class Store:
    max_video_bytes = 20 * 1024 * 1024

    def __init__(self):
        self.assets = {}
        self.data = b"synthetic mp4 bytes"

    async def get_asset(self, asset_id, *, scope):
        if self.assets.get(asset_id) != scope:
            raise AssetNotFound()
        return SimpleNamespace(asset_id=asset_id)

    async def read_bytes(self, asset_id, *, scope):
        await self.get_asset(asset_id, scope=scope)
        return self.data


class Backend:
    def __init__(self, store):
        self.store = store
        self.jobs = {}
        self.keys = {}
        self.release = asyncio.Event()
        self.calls = []
        self.changed = False
        self.cancelled = 0
        self.waits = 0

    async def submit(self, request, *, scope, operation_key):
        if operation_key in self.keys:
            return await self.check_job(self.keys[operation_key], scope=scope)
        if request.reference_asset_id != "image-1":
            if request.action == "edit":
                await self.store.get_asset(request.reference_asset_id, scope=scope)
            else:
                raise AssetNotFound()
        if request.action == "edit" and request.reference_asset_id == "image-1":
            raise InvalidRequest()
        self.calls.append(request)
        job = Job("job-" + str(len(self.jobs) + 1))
        self.jobs[job.job_id] = (job, scope)
        self.keys[operation_key] = job.job_id
        return job

    async def check_job(self, job_id, *, scope):
        if self.changed:
            raise AuthorizationChanged()
        if job_id not in self.jobs or self.jobs[job_id][1] != scope:
            raise AssetNotFound()
        return self.jobs[job_id][0]

    async def poll(self, job_id, *, scope, **kwargs):
        return await self.check_job(job_id, scope=scope)


@pytest.fixture
async def environment(tmp_path):
    store = Store()
    backend = Backend(store)
    runtime = SimpleNamespace(
        config={},
        closed=False,
        owner_id="test-owner",
        videos=backend,
        video_assets=store,
        allowed_roots=(),
        assets=SimpleNamespace(),
    )
    provider = object.__new__(GrokOAuthProvider)
    provider._runtime = runtime
    provider._closed = False
    sends = []

    async def forbidden_send(*args):
        sends.append(args)
        raise AssertionError("Video capability service must never send messages")

    async def current(umo):
        return provider

    async def cid(umo):
        return "conversation-" + umo.rsplit(":", 1)[-1]

    context = SimpleNamespace(
        get_using_provider_async=current,
        get_provider_by_id=lambda id: provider if id == "grok" else None,
        conversation_manager=SimpleNamespace(get_curr_conversation_id=cid),
    )

    context.send_message = forbidden_send
    imports = []

    async def import_reference(ref, *, scope, allowed_roots):
        imports.append((ref, scope))
        return "image-1"

    runtime.assets.import_reference = import_reference
    image_tools = ImageToolService(context, runtime, None)
    service = VideoToolService(context, runtime, image_tools)
    yield SimpleNamespace(
        service=service,
        runtime=runtime,
        backend=backend,
        store=store,
        context=context,
        provider=provider,
        sends=sends,
        imports=imports,
    )
    await service.close()
    assert not sends


async def test_generate_status_and_edit_return_handles_without_delivery(environment):
    env = environment
    event = Event()
    first = json.loads(
        await env.service.generate(event, prompt="animate", reference_asset_id="image-1")
    )
    assert first["status"] == "pending" and first["delivery_owner"] == "caller"
    assert first["job_id"] and not event.sends and not env.sends
    job = env.backend.jobs[first["job_id"]][0]
    job.status, job.asset_id = "done", "video-1"
    env.store.assets[job.asset_id] = await env.service.scope(event)
    status = json.loads(await env.service.status(event, job.job_id))
    assert status["status"] == "done" and status["asset_id"] == "video-1"
    assert status["delivery_owner"] == "caller" and "base64" not in json.dumps(status)
    assert "path" not in status and "url" not in status and "bytes" not in status
    edited = json.loads(
        await env.service.edit(Event(message_id="m2"), prompt="edit", reference_asset_id="video-1")
    )
    assert edited["status"] == "pending" and edited["delivery_owner"] == "caller"
    assert env.backend.calls[-1].action == "edit" and not env.sends and not event.sends
    assert not env.service._foreground


async def test_concurrent_duplicate_requests_preserve_backend_idempotency(environment):
    env = environment
    results = await asyncio.gather(
        *(
            env.service.generate(Event(), prompt="same", reference_asset_id="image-1")
            for _ in range(10)
        )
    )
    assert len(env.backend.calls) == 1
    assert len({json.loads(result)["job_id"] for result in results}) == 1
    defaults = json.loads(
        await env.service.generate(Event(), prompt="same", reference_asset_id="image-1", duration=6)
    )
    assert defaults["job_id"] == json.loads(results[0])["job_id"]
    # No adapter-owned background queue imposes additional pending-job limits.
    for index in range(3):
        result = json.loads(
            await env.service.generate(
                Event(message_id=f"other-{index}"), prompt="new", reference_asset_id="image-1"
            )
        )
        assert result["status"] == "pending"
    assert len(env.backend.calls) == 4 and not env.sends and not env.service._foreground


@pytest.mark.parametrize("setting", ["videos_enabled", "tools_enabled"])
async def test_disabled_permissions_prevent_submission_and_remove_tools(environment, setting):
    env = environment
    env.runtime.config[setting] = False
    result = json.loads(
        await env.service.generate(Event(), prompt="animate", reference_asset_id="image-1")
    )
    assert result["error"] == "PermissionDenied" and not env.backend.calls
    req = SimpleNamespace(func_tool=ToolSet(env.service.build_tools()), extra_user_content_parts=[])
    await env.service.prepare(Event(), req)
    assert not req.func_tool.tools


async def test_foreign_provider_and_unproven_executor_are_denied(environment):
    env = environment

    async def other(umo):
        return object()

    env.context.get_using_provider_async = other
    denied = json.loads(
        await env.service.generate(Event(), prompt="animate", reference_asset_id="image-1")
    )
    assert denied["error"] == "PermissionDenied"
    env.runtime.config["tools_provider_id"] = "grok"
    wrapper = ContextWrapper(context=SimpleNamespace(event=Event()), tool_call_timeout=5)
    outputs = [
        item
        async for item in FunctionToolExecutor.execute(
            env.service.build_tools()[0], wrapper, prompt="executor", reference_asset_id="image-1"
        )
    ]
    assert any(
        json.loads(part.text)["delivery_owner"] == "caller"
        for output in outputs
        if output
        for part in output.content
        if part.type == "text"
    )
    env.runtime.config.clear()
    denied = json.loads(
        await env.service._tool_generate(Event(), prompt="raw", reference_asset_id="image-1")
    )
    assert denied["error"] == "PermissionDenied" and len(env.backend.calls) == 1


async def test_model_call_proof_is_one_use_and_status_uses_actual_calling_provider(environment):
    env = environment
    arguments = {"prompt": "proof", "reference_asset_id": "image-1"}
    issue_tool_call(env.provider, "grok_video_generate", arguments)
    first = json.loads(await env.service._tool_generate(Event(), **arguments))
    again = json.loads(await env.service._tool_generate(Event(), **arguments))
    assert first["status"] == "pending" and again["error"] == "PermissionDenied"
    status_args = {"job_id": first["job_id"]}
    issue_tool_call(env.provider, "grok_video_status", status_args)

    async def foreign_primary(umo):
        return object()

    env.context.get_using_provider_async = foreign_primary
    result = json.loads(await env.service._tool_status(Event(), **status_args))
    assert result["status"] == "pending" and result["delivery_owner"] == "caller"
    assert (
        json.loads(await env.service._tool_status(Event(), **status_args))["error"]
        == "PermissionDenied"
    )


async def test_cross_conversation_jobs_and_video_assets_are_private(environment):
    env = environment
    event = Event()
    result = json.loads(
        await env.service.generate(event, prompt="animate", reference_asset_id="image-1")
    )
    foreign = Event("test:GroupMessage:2")
    assert (
        json.loads(await env.service.status(foreign, result["job_id"]))["error"] == "AssetNotFound"
    )
    job = env.backend.jobs[result["job_id"]][0]
    job.status, job.asset_id = "done", "video-1"
    env.store.assets[job.asset_id] = await env.service.scope(event)
    bad = json.loads(await env.service.edit(foreign, prompt="steal", reference_asset_id="video-1"))
    assert bad["error"] == "AssetNotFound" and len(env.backend.calls) == 1
    assert not event.sends and not foreign.sends


async def test_reloaded_adapter_neither_polls_nor_sends_automatically(environment):
    env = environment
    first = json.loads(
        await env.service.generate(Event(), prompt="animate", reference_asset_id="image-1")
    )
    await env.service.close()

    async def forbidden_poll(*args, **kwargs):
        raise AssertionError("Constructing an adapter must not start polling")

    original = env.backend.poll
    env.backend.poll = forbidden_poll
    reopened = VideoToolService(env.context, env.runtime, env.service.image_tools)
    try:
        assert not reopened._foreground
        repeated = json.loads(
            await reopened.generate(Event(), prompt="animate", reference_asset_id="image-1")
        )
        assert first["job_id"] == repeated["job_id"] and len(env.backend.calls) == 1
        env.backend.poll = original
        assert (
            json.loads(await reopened.status(Event(), first["job_id"]))["delivery_owner"]
            == "caller"
        )
    finally:
        await reopened.close()


@pytest.mark.parametrize("outcome", ["failed", "pending", "error"])
async def test_polling_failure_has_no_notification_or_delivery_side_effects(environment, outcome):
    env = environment
    from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError

    event = Event()
    first = json.loads(
        await env.service.generate(event, prompt="animate", reference_asset_id="image-1")
    )

    async def result(job_id, *, scope, **kwargs):
        job = await env.backend.check_job(job_id, scope=scope)
        if outcome == "error":
            raise ProtocolError("https://secret.invalid/private")
        job.status = outcome
        return job

    env.backend.poll = result
    status = json.loads(await env.service.status(event, first["job_id"]))
    assert status["status"] == ("error" if outcome == "error" else outcome)
    assert "secret.invalid" not in json.dumps(status)
    assert not env.sends and not event.sends and not env.service._foreground


async def test_account_switch_blocks_existing_job_query_without_messages(environment):
    env = environment
    result = json.loads(
        await env.service.generate(Event(), prompt="animate", reference_asset_id="image-1")
    )
    env.backend.changed = True
    status = json.loads(await env.service.status(Event(), result["job_id"]))
    assert status["error"] == "AuthorizationChanged" and not env.sends


@pytest.mark.parametrize("operation", ["submit", "poll"])
async def test_close_cancels_only_owned_foreground_without_orphan_tasks(environment, operation):
    env = environment
    first = json.loads(
        await env.service.generate(Event(), prompt="animate", reference_asset_id="image-1")
    )
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    if operation == "submit":
        env.backend.submit = slow
        task = asyncio.create_task(
            env.service.generate(
                Event(message_id="m2"), prompt="animate", reference_asset_id="image-1"
            )
        )
    else:
        env.backend.poll = slow
        task = asyncio.create_task(env.service.status(Event(), first["job_id"]))
    await started.wait()
    await env.service.close()
    assert cancelled.is_set() and task.cancelled() and not env.service._foreground
    assert not env.sends
    denied = json.loads(
        await env.service.generate(Event(), prompt="animate", reference_asset_id="image-1")
    )
    assert denied["error"] == "ServiceClosed"


@pytest.mark.parametrize(
    "arguments",
    [
        {"duration": True},
        {"duration": 6.0},
        {"duration": 60},
        {"url": "https://foreign.invalid"},
        {"provider_id": "other"},
    ],
)
async def test_unknown_arguments_and_invalid_duration_do_not_submit(environment, arguments):
    env = environment
    result = json.loads(
        await env.service.generate(
            Event(), prompt="animate", reference_asset_id="image-1", **arguments
        )
    )
    assert result["error"] == "InvalidRequest" and not env.backend.calls


async def test_reference_discovery_works_independently_of_image_tools(environment):
    env = environment
    env.runtime.config["images_enabled"] = False
    event = Event()
    event.message_obj.message = [Image.fromURL("https://example.invalid/image.png")]
    req = SimpleNamespace(func_tool=ToolSet(env.service.build_tools()), extra_user_content_parts=[])
    await env.service.prepare(event, req)
    assert len(req.func_tool.tools) == 3 and len(env.imports) == 1
    assert "image-1" in req.extra_user_content_parts[0].text


async def test_matching_image_discovery_cache_avoids_duplicate_import(environment):
    env = environment
    event = Event()
    event.message_obj.message = [Image.fromURL("https://example.invalid/image.png")]
    event.set_extra(
        "grok_imported_image_assets",
        {"scope": await env.service.scope(event), "asset_ids": ["image-1"]},
    )
    req = SimpleNamespace(func_tool=ToolSet(env.service.build_tools()), extra_user_content_parts=[])
    await env.service.prepare(event, req)
    assert not env.imports and "image-1" in req.extra_user_content_parts[0].text
    event.unified_msg_origin = "test:GroupMessage:2"
    req.extra_user_content_parts.clear()
    await env.service.prepare(event, req)
    assert len(env.imports) == 1
