"""Caller-owned video API against real scoped services and AstrBot provider lifecycle."""

import asyncio
import base64
import time
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest
from astrbot_plugin_grok_oauth.astrbot_adapter.provider import GrokOAuthProvider
from astrbot_plugin_grok_oauth.grok_oauth.errors import (
    AssetNotFound,
    AuthorizationChanged,
    InvalidRequest,
    PermissionDenied,
    ProtocolError,
    ServiceClosed,
    UnsafeMediaSource,
)
from astrbot_plugin_grok_oauth.grok_oauth.media import AssetStore
from astrbot_plugin_grok_oauth.grok_oauth.models import TokenSnapshot
from astrbot_plugin_grok_oauth.grok_oauth.video_store import MAX_VIDEO_BYTES, VideoStore
from astrbot_plugin_grok_oauth.grok_oauth.videos import VideoService
from PIL import Image


def mp4():
    def box(kind, content):
        return (len(content) + 8).to_bytes(4, "big") + kind + content

    header = bytes(12) + (1000).to_bytes(4, "big") + (6000).to_bytes(4, "big")
    return (
        box(b"ftyp", b"isom" + bytes(4) + b"mp42")
        + box(b"moov", box(b"mvhd", header))
        + box(b"mdat", bytes(24))
    )


class Event:
    unified_msg_origin = "qq:GroupMessage:123"
    platform_id = "qq"

    def get_platform_id(self):
        return self.platform_id


class OAuth:
    binding_generation = 1

    def __init__(self):
        self.token = TokenSnapshot(
            "default",
            "TEST",
            "TEST-REFRESH",
            time.time() + 999,
            "",
            "client",
            epoch=1,
            user_id="account",
        )

    async def get_token(self):
        return self.token


class Http:
    def __init__(self):
        self.calls = []
        self.pending = False
        self.gate = None
        self.entered = asyncio.Event()

    async def request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json")))
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        if method == "POST":
            return {"request_id": "remote-" + str(len(self.calls))}
        if self.pending:
            return {"status": "pending"}
        return {"status": "done", "video": {"url": "https://vidgen.x.ai/secret?sig=private"}}


class Downloader:
    async def fetch(self, url, *, deadline):
        return mp4()

    async def close(self):
        pass


@pytest.fixture
async def env(tmp_path):
    images = AssetStore(tmp_path / "images")
    store = VideoStore(tmp_path / "videos", media_downloader=Downloader())
    oauth = OAuth()
    http = Http()
    service = VideoService(http, images, store, oauth=oauth)
    conversation = SimpleNamespace(value="conversation-1")

    async def current(umo):
        return conversation.value

    async def forbidden_send(*args, **kwargs):
        raise AssertionError("The generation provider must not send messages")

    runtime = SimpleNamespace(
        config={"tools_enabled": False},
        closed=False,
        videos=service,
        video_assets=store,
        assets=images,
        allowed_roots=(tmp_path,),
        oauth=oauth,
        host_context=SimpleNamespace(
            conversation_manager=SimpleNamespace(get_curr_conversation_id=current),
            send_message=forbidden_send,
        ),
    )
    provider = object.__new__(GrokOAuthProvider)
    provider._runtime = runtime
    provider._closed = False
    provider._tasks = set()
    provider.provider_config = {"id": "grok/provider-1"}
    image = BytesIO()
    Image.new("RGB", (8, 8)).save(image, "PNG")
    reference = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    yield SimpleNamespace(
        provider=provider,
        runtime=runtime,
        service=service,
        store=store,
        oauth=oauth,
        http=http,
        event=Event(),
        conversation=conversation,
        reference=reference,
    )
    await provider.terminate()
    await service.close()
    await store.close()
    await images.close()


async def submit(env, *, key="caller:message:1"):
    asset = await env.provider.import_video_image(env.reference, event=env.event)
    return await env.provider.submit_video(
        "animate",
        asset,
        event=env.event,
        operation_key=key,
    ), asset


async def completed(env):
    job, _ = await submit(env)
    return await env.provider.get_video_job(job["job_id"], event=env.event)


async def test_public_generate_edit_query_wait_read_and_explicit_caller_delivery(env):
    job, asset = await submit(env)
    assert job["status"] == "pending" and job["delivery_owner"] == "caller"
    again = await env.provider.submit_video(
        "animate",
        asset,
        event=env.event,
        operation_key="caller:message:1",
    )
    assert again == job
    done = await env.provider.get_video_job(job["job_id"], event=env.event)
    assert done["status"] == "done" and done["asset_id"]
    assert set(done) == {
        "job_id",
        "status",
        "asset_id",
        "error",
        "model",
        "duration",
        "delivery_owner",
        "submission_state",
    }
    assert await env.provider.wait_video_job(job["job_id"], event=env.event) == done
    assert await env.provider.read_video_bytes(job["job_id"], event=env.event) == mp4()
    edit = await env.provider.edit_video(
        "make it blue",
        done["asset_id"],
        event=env.event,
        operation_key="caller:edit:1",
    )
    edit_done = await env.provider.wait_video_job(edit["job_id"], event=env.event)
    assert edit_done["status"] == "done" and edit_done["model"] == "grok-imagine-video"
    assert [call[1] for call in env.http.calls if call[0] == "POST"] == [
        "/videos/generations",
        "/videos/edits",
    ]
    assert not env.provider._tasks


@pytest.mark.parametrize("changed", ["prompt", "duration", "reference", "action"])
async def test_operation_key_cannot_charge_again_with_changed_arguments(env, changed):
    _, asset = await submit(env)
    second_asset = await env.provider.import_video_image(env.reference, event=env.event)
    with pytest.raises(ProtocolError):
        if changed == "action":
            await env.provider.edit_video(
                "animate",
                asset,
                event=env.event,
                operation_key="caller:message:1",
            )
        else:
            await env.provider.submit_video(
                "changed" if changed == "prompt" else "animate",
                second_asset if changed == "reference" else asset,
                duration=10 if changed == "duration" else 6,
                event=env.event,
                operation_key="caller:message:1",
            )
    assert sum(call[0] == "POST" for call in env.http.calls) == 1


@pytest.mark.parametrize("key", ["", " ", None, False, 1, "x" * 257])
async def test_invalid_operation_key_is_rejected_before_paid_request(env, key):
    asset = await env.provider.import_video_image(env.reference, event=env.event)
    with pytest.raises(InvalidRequest):
        await env.provider.submit_video("animate", asset, event=env.event, operation_key=key)
    assert not env.http.calls


async def test_operation_keys_are_namespaced_by_selected_provider(env):
    first, asset = await submit(env)
    env.provider.provider_config["id"] = "grok/provider-2"
    second = await env.provider.submit_video(
        "animate",
        asset,
        event=env.event,
        operation_key="caller:message:1",
    )
    assert first["job_id"] != second["job_id"]
    assert sum(call[0] == "POST" for call in env.http.calls) == 2


@pytest.mark.parametrize("changed", ["conversation", "umo", "platform"])
async def test_jobs_and_bytes_remain_in_original_session_scope(env, changed):
    done = await completed(env)
    if changed == "conversation":
        env.conversation.value = "new-conversation"
    elif changed == "umo":
        env.event.unified_msg_origin = "qq:GroupMessage:other"
    else:
        env.event.platform_id = "other"
        env.event.unified_msg_origin = "other:GroupMessage:123"
    for name in ("get_video_job", "wait_video_job", "read_video_bytes"):
        with pytest.raises(AssetNotFound):
            await getattr(env.provider, name)(done["job_id"], event=env.event)
    with pytest.raises(AssetNotFound):
        await env.provider.edit_video(
            "change",
            done["asset_id"],
            event=env.event,
            operation_key="new-edit",
        )


@pytest.mark.parametrize(
    "method",
    [
        "import_video_image",
        "submit_video",
        "edit_video",
        "get_video_job",
        "wait_video_job",
        "read_video_bytes",
    ],
)
@pytest.mark.parametrize("state", ["disabled", "provider_closed", "runtime_closed"])
async def test_all_public_methods_honor_feature_and_lifecycle_guards(env, method, state):
    if state == "disabled":
        env.runtime.config["videos_enabled"] = False
    elif state == "provider_closed":
        env.provider._closed = True
    else:
        env.runtime.closed = True
    args = [env.reference] if method == "import_video_image" else ["job-id"]
    kwargs = {"event": env.event}
    if method in {"submit_video", "edit_video"}:
        args = ["prompt", "asset-id"]
        kwargs["operation_key"] = "key"
    with pytest.raises(PermissionDenied if state == "disabled" else ServiceClosed):
        await getattr(env.provider, method)(*args, **kwargs)
    assert not env.http.calls and not env.provider._tasks


@pytest.mark.parametrize("invalid", ["context", "conversation", "event", "prefix"])
async def test_missing_or_inconsistent_session_is_rejected(env, invalid):
    if invalid == "context":
        env.runtime.host_context = None
    elif invalid == "conversation":
        env.conversation.value = None
    elif invalid == "event":
        env.event = object()
    else:
        env.event.unified_msg_origin = "foreign:GroupMessage:123"
    with pytest.raises(InvalidRequest):
        await env.provider.import_video_image(env.reference, event=env.event)
    assert not env.http.calls


async def test_read_pending_job_does_not_poll_or_return_media(env):
    job, _ = await submit(env)
    with pytest.raises(InvalidRequest):
        await env.provider.read_video_bytes(job["job_id"], event=env.event)
    assert len(env.http.calls) == 1


async def test_account_change_blocks_query_edit_and_read(env):
    done = await completed(env)
    env.oauth.token = replace(env.oauth.token, epoch=2, user_id="other-account")
    for name in ("get_video_job", "wait_video_job", "read_video_bytes"):
        with pytest.raises(AuthorizationChanged):
            await getattr(env.provider, name)(done["job_id"], event=env.event)
    with pytest.raises(AuthorizationChanged):
        await env.provider.edit_video(
            "change",
            done["asset_id"],
            event=env.event,
            operation_key="edit-other-account",
        )
    assert sum(call[0] == "POST" for call in env.http.calls) == 1


@pytest.mark.parametrize("change", ["account", "generation"])
async def test_read_rechecks_account_after_loading_bytes(env, change, monkeypatch):
    done = await completed(env)
    original = env.store.read_bytes

    async def changed(*args, **kwargs):
        content = await original(*args, **kwargs)
        if change == "account":
            env.oauth.token = replace(env.oauth.token, epoch=2)
        else:
            env.oauth.binding_generation += 1
        return content

    monkeypatch.setattr(env.store, "read_bytes", changed)
    with pytest.raises(AuthorizationChanged):
        await env.provider.read_video_bytes(done["job_id"], event=env.event)
    assert not any(env.store._leases.values())


async def test_read_api_enforces_20mib_ceiling_independent_of_store_setting(env, monkeypatch):
    done = await completed(env)
    original = env.store.get_asset

    async def oversized(*args, **kwargs):
        return replace(await original(*args, **kwargs), size_bytes=MAX_VIDEO_BYTES + 1)

    monkeypatch.setattr(env.store, "get_asset", oversized)
    with pytest.raises(UnsafeMediaSource):
        await env.provider.read_video_bytes(done["job_id"], event=env.event)


async def test_termination_cancels_active_wait_and_durable_job_remains_recoverable(env):
    job, asset = await submit(env)
    env.http.gate = asyncio.Event()
    env.http.entered.clear()
    waiting = asyncio.create_task(env.provider.wait_video_job(job["job_id"], event=env.event))
    await env.http.entered.wait()
    await env.provider.terminate()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not env.provider._tasks
    env.http.gate = None
    env.provider._closed = False
    recovered = await env.provider.submit_video(
        "animate",
        asset,
        event=env.event,
        operation_key="caller:message:1",
    )
    assert recovered["job_id"] == job["job_id"]
    assert (await env.provider.get_video_job(job["job_id"], event=env.event))["status"] == "done"
    assert sum(call[0] == "POST" for call in env.http.calls) == 1
