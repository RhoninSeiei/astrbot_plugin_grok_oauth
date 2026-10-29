import asyncio
import base64
import json
import time
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from grok_oauth.errors import (
    AssetNotFound,
    AuthorizationChanged,
    Busy,
    InvalidRequest,
    OutcomeUnknown,
    ProtocolError,
    UnsafeMediaSource,
)
from grok_oauth.http import AuthorizedHttp
from grok_oauth.media import AssetStore
from grok_oauth.models import AssetScope, RequestPolicy, TokenSnapshot
from grok_oauth.video_store import VideoStore, mp4_duration
from grok_oauth.videos import EDIT_MODEL, GENERATION_MODEL, VideoRequest, VideoService


def box(kind, content):
    return (len(content) + 8).to_bytes(4, "big") + kind + content


def mp4(duration=6):
    header = bytes(12) + (1000).to_bytes(4, "big") + int(duration * 1000).to_bytes(4, "big")
    return (
        box(b"ftyp", b"isom" + bytes(4) + b"mp42")
        + box(b"moov", box(b"mvhd", header))
        + box(b"mdat", bytes(24))
    )


class OAuth:
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

    async def get_token(self, **kwargs):
        return self.token


class Downloader:
    def __init__(self):
        self.calls = []
        self.content = mp4()
        self.error = None
        self.closed = False

    async def fetch(self, url, *, deadline):
        self.calls.append((url, deadline))
        if self.error:
            raise self.error
        return self.content

    async def close(self):
        self.closed = True


class Http:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.gate = None
        self.called = asyncio.Event()

    async def request_json(self, method, path, *, json=None, policy, **kwargs):
        self.calls.append((method, path, json, policy))
        self.called.set()
        if self.gate:
            await self.gate.wait()
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
async def bundle(tmp_path):
    scope = AssetScope("qq", "group:1", "conversation")
    image_assets = AssetStore(tmp_path / "images")
    output = BytesIO()
    Image.new("RGB", (8, 8)).save(output, "PNG")
    ref = await image_assets.store_bytes(output.getvalue(), scope=scope, request_id="input")
    downloader = Downloader()
    store = VideoStore(tmp_path / "videos", media_downloader=downloader)
    http = Http()
    oauth = OAuth()
    service = VideoService(http, image_assets, store, oauth=oauth)
    yield SimpleNamespace(
        scope=scope,
        images=image_assets,
        image=ref,
        downloader=downloader,
        store=store,
        http=http,
        oauth=oauth,
        service=service,
    )
    await service.close()
    await store.close()
    await image_assets.close()


def request(bundle, **kwargs):
    return VideoRequest("move slowly", bundle.image.asset_id, **kwargs)


async def submit(bundle, key="message-1"):
    bundle.http.responses.append({"request_id": "task-1"})
    return await bundle.service.submit(request(bundle), scope=bundle.scope, operation_key=key)


@pytest.mark.asyncio
async def test_generation_polling_and_result_reuse(bundle):
    job = await submit(bundle)
    assert job.status == "pending"
    method, path, body, policy = bundle.http.calls[0]
    assert (method, path) == ("POST", "/videos/generations")
    assert body["model"] == GENERATION_MODEL
    assert body["duration"] == 6 and body["resolution"] == "480p"
    assert body["image"]["url"].startswith("data:image/png;base64,")
    assert policy.side_effecting and policy.safe_pre_send_retries == 0
    bundle.http.responses.extend(
        [
            {"status": "pending"},
            {"status": "done", "video": {"url": "https://vidgen.x.ai/result?private=secret"}},
        ]
    )
    assert (await bundle.service.poll(job.job_id, scope=bundle.scope)).status == "pending"
    done = await bundle.service.poll(job.job_id, scope=bundle.scope)
    assert done.status == "done" and done.asset_id and done.duration == 6
    assert await bundle.store.read_bytes(done.asset_id, scope=bundle.scope) == mp4()
    assert (
        base64.b64decode(await bundle.store.to_base64(done.asset_id, scope=bundle.scope)) == mp4()
    )
    assert (await bundle.service.poll(job.job_id, scope=bundle.scope)).asset_id == done.asset_id
    assert len(bundle.http.calls) == 3
    assert "private" not in bundle.store._metadata.read_text()
    assert "TEST" not in bundle.store._metadata.read_text()
    assert "request_id" not in done.public()


@pytest.mark.asyncio
async def test_edit_payload_and_duration_bound(bundle):
    asset = await bundle.store.store_bytes(
        mp4(), scope=bundle.scope, binding=await bundle.service._binding()
    )
    bundle.http.responses.append({"request_id": "edited-1"})
    await bundle.service.submit(
        VideoRequest("change background", asset.asset_id, action="edit"), scope=bundle.scope
    )
    method, path, body, _ = bundle.http.calls[0]
    assert (method, path) == ("POST", "/videos/edits")
    assert body == {
        "model": EDIT_MODEL,
        "prompt": "change background",
        "video": {"url": "data:video/mp4;base64," + base64.b64encode(mp4()).decode()},
    }
    long = await bundle.store.store_bytes(
        mp4(10), scope=bundle.scope, binding=await bundle.service._binding()
    )
    with pytest.raises(InvalidRequest):
        await bundle.service.submit(
            VideoRequest("edit", long.asset_id, action="edit"), scope=bundle.scope
        )
    assert len(bundle.http.calls) == 1


@pytest.mark.asyncio
async def test_same_operation_deduplicates_across_store_reload(bundle):
    job = await submit(bundle)
    await bundle.store.close()
    bundle.store = VideoStore(bundle.store.root, media_downloader=bundle.downloader)
    bundle.service.video_store = bundle.store
    result = await bundle.service.submit(
        request(bundle), scope=bundle.scope, operation_key="message-1"
    )
    assert result.job_id == job.job_id and result.request_id == "task-1"
    assert len(bundle.http.calls) == 1
    with pytest.raises(ProtocolError):
        await bundle.service.submit(
            VideoRequest("different", bundle.image.asset_id),
            scope=bundle.scope,
            operation_key="message-1",
        )


@pytest.mark.asyncio
async def test_ambiguous_submit_never_retries(bundle):
    bundle.http.responses.append(OutcomeUnknown())
    with pytest.raises(OutcomeUnknown) as captured:
        await bundle.service.submit(request(bundle), scope=bundle.scope, operation_key="ambiguous")
    job_id = captured.value.operation_id
    assert (await bundle.service.check_job(job_id, scope=bundle.scope)).status == "unknown"
    again = await bundle.service.submit(
        request(bundle), scope=bundle.scope, operation_key="ambiguous"
    )
    assert again.job_id == job_id and again.status == "unknown"
    assert len(bundle.http.calls) == 1


@pytest.mark.asyncio
async def test_cancel_submit_durable_unknown_and_shutdown(bundle):
    bundle.http.gate = asyncio.Event()
    task = asyncio.create_task(
        bundle.service.submit(request(bundle), scope=bundle.scope, operation_key="cancelled")
    )
    await bundle.http.called.wait()
    await bundle.service.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = await bundle.store.list_jobs(scope=bundle.scope)
    assert len(records) == 1 and records[0].status == "unknown"
    assert len(bundle.http.calls) == 1


@pytest.mark.asyncio
async def test_done_download_failure_is_resumable_without_post(bundle):
    job = await submit(bundle)
    bundle.http.responses.extend(
        [{"status": "done", "video": {"url": "https://vidgen.x.ai/result"}}] * 2
    )
    bundle.downloader.error = UnsafeMediaSource()
    with pytest.raises(UnsafeMediaSource):
        await bundle.service.poll(job.job_id, scope=bundle.scope)
    assert (await bundle.store.get_job(job.job_id, scope=bundle.scope)).request_id == "task-1"
    bundle.downloader.error = None
    assert (await bundle.service.poll(job.job_id, scope=bundle.scope)).status == "done"
    assert [call[0] for call in bundle.http.calls] == ["POST", "GET", "GET"]


@pytest.mark.asyncio
async def test_wait_deadline_preserves_pending_task(bundle):
    job = await submit(bundle)
    bundle.http.responses.extend([{"status": "pending"}] * 20)
    result = await bundle.service.wait(
        job.job_id, scope=bundle.scope, timeout=0.03, poll_interval=0.01
    )
    assert result.status == "pending" and result.request_id == "task-1"
    assert all(c[0] == "GET" for c in bundle.http.calls[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"status": "unexpected"}, {"status": "done", "video": {}}, {}])
async def test_invalid_poll_keeps_remote_handle(bundle, payload):
    job = await submit(bundle)
    bundle.http.responses.append(payload)
    with pytest.raises(ProtocolError):
        await bundle.service.poll(job.job_id, scope=bundle.scope)
    assert (await bundle.store.get_job(job.job_id, scope=bundle.scope)).request_id == "task-1"


@pytest.mark.asyncio
async def test_failed_result_does_not_expose_upstream_error(bundle):
    job = await submit(bundle)
    bundle.http.responses.append({"status": "failed", "error": {"message": "PRIVATE"}})
    result = await bundle.service.poll(job.job_id, scope=bundle.scope)
    assert result.status == "failed" and result.error == "VideoGenerationFailed"
    assert "PRIVATE" not in json.dumps(result.public())


@pytest.mark.asyncio
async def test_scope_and_account_switch_rejected_before_poll_or_edit(bundle):
    job = await submit(bundle)
    other_scope = AssetScope("qq", "group:2", "conversation")
    with pytest.raises(AssetNotFound):
        await bundle.service.poll(job.job_id, scope=other_scope)
    asset = await bundle.store.store_bytes(
        mp4(), scope=bundle.scope, binding=await bundle.service._binding()
    )
    bundle.oauth.token = TokenSnapshot(
        "default", "OTHER", "OTHER", None, "", "client", epoch=2, user_id="new"
    )
    with pytest.raises(AuthorizationChanged):
        await bundle.service.check_job(job.job_id, scope=bundle.scope)
    with pytest.raises(AuthorizationChanged):
        await bundle.service.submit(
            VideoRequest("edit", asset.asset_id, action="edit"), scope=bundle.scope
        )
    assert len(bundle.http.calls) == 1


@pytest.mark.asyncio
async def test_asset_scope_size_and_integrity(bundle, tmp_path):
    asset = await bundle.store.store_bytes(mp4(), scope=bundle.scope)
    with pytest.raises(AssetNotFound):
        await bundle.store.read_bytes(
            asset.asset_id, scope=AssetScope("qq", "other", "conversation")
        )
    small = VideoStore(tmp_path / "small", max_video_bytes=16, media_downloader=Downloader())
    try:
        with pytest.raises(UnsafeMediaSource):
            await small.store_bytes(mp4(), scope=bundle.scope)
    finally:
        await small.close()
    from pathlib import Path

    await asyncio.to_thread(Path(asset.path).write_bytes, mp4(7))
    with pytest.raises(UnsafeMediaSource):
        await bundle.store.read_bytes(asset.asset_id, scope=bundle.scope)


@pytest.mark.parametrize(
    "content", [b"not-mp4", box(b"ftyp", b"isom" + bytes(4)), box(b"mdat", b"data"), mp4()[:-1]]
)
def test_invalid_mp4_rejected(content):
    with pytest.raises(UnsafeMediaSource):
        mp4_duration(content)


@pytest.mark.asyncio
async def test_empty_202_video_poll_supported_and_no_general_json_change():
    oauth = OAuth()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(202, content=b""))
    ) as client:
        http = AuthorizedHttp(client, oauth)
        assert await http.request_json("GET", "/videos/task-1", policy=RequestPolicy()) == {
            "status": "pending"
        }
        with pytest.raises(ProtocolError):
            await http.request_json("GET", "/models", policy=RequestPolicy())
        with pytest.raises(OutcomeUnknown):
            await http.request_json(
                "POST", "/videos/generations", json={}, policy=RequestPolicy(side_effecting=True)
            )


@pytest.mark.asyncio
async def test_store_capacity_bounds_jobs(bundle, tmp_path):
    bounded = VideoStore(tmp_path / "bounded", max_jobs=1, media_downloader=Downloader())
    service = VideoService(bundle.http, bundle.images, bounded, oauth=bundle.oauth)
    bundle.http.responses.append({"request_id": "task-capacity"})
    try:
        await service.submit(request(bundle), scope=bundle.scope, operation_key="one")
        with pytest.raises(Busy):
            await service.submit(request(bundle), scope=bundle.scope, operation_key="two")
        assert len(bundle.http.calls) == 1
    finally:
        await service.close()
        await bounded.close()


def access_token(subject):
    payload = {
        "sub": subject,
        "iss": "https://auth.x.ai",
        "client_id": "client",
        "principal_type": "User",
        "principal_id": subject,
    }
    return (
        "e30."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        + ".signature"
    )


@pytest.mark.asyncio
async def test_legacy_access_identity_and_missing_identity_fail_closed(bundle):
    from dataclasses import replace

    bundle.oauth.token = replace(
        bundle.oauth.token, user_id=None, access_token=access_token("legacy-account")
    )
    job = await submit(bundle)
    assert job.status == "pending"
    bundle.oauth.token = replace(
        bundle.oauth.token, access_token=access_token("other-legacy-account")
    )
    with pytest.raises(AuthorizationChanged):
        await bundle.service.check_job(job.job_id, scope=bundle.scope)
    bundle.oauth.token = replace(bundle.oauth.token, access_token="opaque-no-identity")
    from grok_oauth.errors import ReauthorizationRequired

    with pytest.raises(ReauthorizationRequired):
        await bundle.service.submit(request(bundle), scope=bundle.scope, operation_key="new-opaque")
    assert len(bundle.http.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_identity", [False, True])
async def test_submit_transport_detects_rebinding_before_first_send(bundle, same_identity):
    from dataclasses import replace

    sent = []

    class SwitchingOAuth(OAuth):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.binding_generation = 0

        async def get_token(self, **kwargs):
            self.calls += 1
            if self.calls == 3:
                self.binding_generation += 1
                self.token = replace(
                    self.token,
                    access_token="NEW-TOKEN",
                    user_id="account" if same_identity else "new-account",
                )
            return self.token

    oauth = SwitchingOAuth()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: sent.append(r) or httpx.Response(200, json={"request_id": "bad"})
        )
    ) as client:
        service = VideoService(
            AuthorizedHttp(client, oauth), bundle.images, bundle.store, oauth=oauth
        )
        try:
            with pytest.raises(AuthorizationChanged):
                await service.submit(request(bundle), scope=bundle.scope, operation_key="race")
            assert not sent
        finally:
            await service.close()


@pytest.mark.asyncio
async def test_auth_retry_requires_same_bound_account_and_generation():
    from dataclasses import replace

    from grok_oauth.http import _video_account_binding

    oauth = OAuth()
    original = oauth.token
    calls = []

    async def token(**kwargs):
        if kwargs.get("force_refresh"):
            oauth.token = replace(original, access_token="OTHER", user_id="new-account")
        return oauth.token

    oauth.get_token = token
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(401))
    ) as client:
        with pytest.raises(AuthorizationChanged):
            await AuthorizedHttp(client, oauth).request_json(
                "GET",
                "/videos/task-1",
                policy=RequestPolicy(),
                account_binding=_video_account_binding(original),
                binding_generation=0,
            )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_regular_refresh_keeps_binding_and_polling_authorized():
    from dataclasses import replace

    from grok_oauth.http import _video_account_binding

    oauth = OAuth()
    original = oauth.token
    calls = []

    async def token(**kwargs):
        if kwargs.get("force_refresh"):
            oauth.token = replace(original, access_token="REFRESHED", version=2)
        return oauth.token

    oauth.get_token = token
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: calls.append(r) or httpx.Response(401 if len(calls) == 1 else 202)
        )
    ) as client:
        result = await AuthorizedHttp(client, oauth).request_json(
            "GET",
            "/videos/task-1",
            policy=RequestPolicy(),
            account_binding=_video_account_binding(original),
            binding_generation=0,
        )
    assert result == {"status": "pending"}
    assert len(calls) == 2 and calls[1].headers["authorization"] == "Bearer REFRESHED"


@pytest.mark.asyncio
async def test_video_downloader_content_type_and_credential_free_pinned_request():
    import ipaddress

    from grok_oauth.media_download import PublicMediaDownloader

    requests = []

    async def resolve(host, port):
        assert (host, port) == ("vidgen.x.ai", 443)
        return [ipaddress.ip_address("8.8.8.8")]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                requests.append(r)
                or httpx.Response(200, headers={"content-type": "video/mp4"}, content=mp4())
            )
        )
    ) as client:
        downloader = PublicMediaDownloader(
            ["vidgen.x.ai"],
            client=client,
            resolver=resolve,
            allowed_content_types=("video/mp4", "application/octet-stream"),
            accept="video/mp4",
        )
        assert (
            await downloader.fetch(
                "https://vidgen.x.ai/signed.mp4?secret=value", deadline=time.monotonic() + 5
            )
            == mp4()
        )
        assert requests[0].url.host == "8.8.8.8"
        assert requests[0].headers["host"] == "vidgen.x.ai"
        assert requests[0].headers["accept"] == "video/mp4"
        assert "authorization" not in requests[0].headers
        assert "cookie" not in requests[0].headers


@pytest.mark.asyncio
async def test_acknowledged_submit_disk_failure_retains_task_for_poll_and_dedup(
    bundle, monkeypatch
):
    original_write = bundle.store._write
    writes = 0

    def fail_acknowledged_write(data):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        original_write(data)

    monkeypatch.setattr(bundle.store, "_write", fail_acknowledged_write)
    job = await submit(bundle)
    assert job.status == "pending" and job.request_id == "task-1"
    assert job.error == "CredentialPersistenceError"
    assert (await bundle.store.get_job(job.job_id, scope=bundle.scope)).request_id == "task-1"
    assert (
        await bundle.service.submit(request(bundle), scope=bundle.scope, operation_key="message-1")
    ).job_id == job.job_id
    bundle.http.responses.append({"status": "done", "video": {"url": "https://vidgen.x.ai/result"}})
    done = await bundle.service.poll(job.job_id, scope=bundle.scope)
    assert done.status == "done" and done.error == ""
    assert [c[0] for c in bundle.http.calls] == ["POST", "GET"]
    assert not bundle.store._volatile_jobs
