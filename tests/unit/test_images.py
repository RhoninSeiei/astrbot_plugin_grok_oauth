import asyncio
import base64
from io import BytesIO

import pytest
from PIL import Image

from grok_oauth.errors import Busy, InvalidImageRequest, OutcomeUnknown, ProtocolError
from grok_oauth.images import ImageService
from grok_oauth.media import AssetStore
from grok_oauth.models import AssetScope, ImageRequest


def png(color=(20, 40, 60)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 6), color).save(output, format="PNG")
    return output.getvalue()


class StubHttp:
    def __init__(self, response=None, gate=None):
        self.response = response or {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
        self.gate = gate
        self.calls = []
        self.called = asyncio.Event()

    async def request_json(self, method, path, *, json=None, policy):
        self.calls.append((method, path, json, policy))
        self.called.set()
        if self.gate:
            await self.gate.wait()
        return self.response


@pytest.fixture
def scope():
    return AssetScope("qq", "user:7", "conversation-a")


@pytest.mark.asyncio
async def test_generation_payload_and_size_mapping(tmp_path, scope):
    http = StubHttp()
    assets = AssetStore(tmp_path / "assets")
    try:
        result = await ImageService(http, assets).generate(
            ImageRequest("draw a lighthouse", n=1, size="1024x1024"), scope=scope
        )
        method, path, body, policy = http.calls[0]
        assert (method, path) == ("POST", "/images/generations")
        assert body == {
            "prompt": "draw a lighthouse",
            "model": "grok-imagine-image-2.0",
            "n": 1,
            "response_format": "b64_json",
            "aspect_ratio": "1:1",
            "resolution": "1k",
        }
        assert policy.side_effecting is True
        assert policy.capability == "images"
        assert len(result) == 1 and result[0].mime_type == "image/png"
    finally:
        await assets.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 3, 5])
async def test_edit_preserves_reference_order_for_current_model(tmp_path, scope, count):
    assets = AssetStore(tmp_path / "assets")
    refs = []
    for index in range(count):
        image = await assets.store_bytes(png((index, 0, 0)), scope=scope, request_id="seed")
        refs.append(image.asset_id)
    http = StubHttp()
    try:
        await ImageService(http, assets).generate(
            ImageRequest("combine", reference_images=tuple(refs)), scope=scope
        )
        body = http.calls[0][2]
        key = "image" if count == 1 else "images"
        sent = [body[key]] if count == 1 else body[key]
        expected = []
        for asset_id in refs:
            path = await assets.resolve(asset_id, scope=scope)
            expected.append(
                {
                    "type": "image_url",
                    "url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(),
                }
            )
        assert sent == expected
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_six_references_and_invalid_n_fail_before_http(tmp_path, scope):
    assets = AssetStore(tmp_path / "assets")
    refs = []
    for index in range(6):
        image = await assets.store_bytes(png((index, 0, 0)), scope=scope, request_id="seed")
        refs.append(image.asset_id)
    http = StubHttp()
    service = ImageService(http, assets)
    try:
        with pytest.raises(InvalidImageRequest):
            await service.generate(
                ImageRequest("combine", reference_images=tuple(refs)), scope=scope
            )
        for invalid in (0, -1, 4.0, True, 5):
            with pytest.raises(InvalidImageRequest):
                await service.generate(ImageRequest("draw", n=invalid), scope=scope)
        for invalid_request in (
            ImageRequest("draw", action=False),
            ImageRequest("draw", size=[]),
            ImageRequest("draw", aspect_ratio=True),
            ImageRequest("draw", resolution={}),
        ):
            with pytest.raises(InvalidImageRequest):
                await service.generate(invalid_request, scope=scope)
        assert http.calls == []
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_legacy_model_keeps_three_reference_limit(tmp_path, scope):
    assets = AssetStore(tmp_path / "assets")
    refs = []
    for index in range(4):
        refs.append(
            (await assets.store_bytes(png((index, 0, 0)), scope=scope, request_id="seed")).asset_id
        )
    http = StubHttp()
    try:
        with pytest.raises(InvalidImageRequest):
            await ImageService(http, assets).generate(
                ImageRequest("combine", model="grok-imagine-image", reference_images=tuple(refs)),
                scope=scope,
            )
        assert http.calls == []
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_partial_batch_failure_exposes_completed_assets(tmp_path, scope):
    response = {
        "model": "grok-imagine-image-2.0",
        "data": [
            {"b64_json": base64.b64encode(png()).decode(), "revised_prompt": "safe"},
            {"b64_json": "not-base64"},
        ],
    }
    assets = AssetStore(tmp_path / "assets")
    try:
        with pytest.raises(ProtocolError) as error:
            await ImageService(StubHttp(response), assets).generate(
                ImageRequest("draw", n=2), scope=scope
            )
        assert error.value.partial is True
        assert len(error.value.assets) == 1
        assert await assets.resolve(error.value.assets[0].asset_id, scope=scope)
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_queue_deadline_and_cancel_do_not_leak_capacity(tmp_path, scope):
    gate = asyncio.Event()
    http = StubHttp(gate=gate)
    assets = AssetStore(tmp_path / "assets")
    service = ImageService(http, assets, max_running=1, max_pending=1)
    first = asyncio.create_task(service.generate(ImageRequest("one", timeout=1), scope=scope))
    await http.called.wait()
    waiting = asyncio.create_task(service.generate(ImageRequest("two", timeout=1), scope=scope))
    await asyncio.sleep(0)
    with pytest.raises(Busy):
        await service.generate(ImageRequest("three", timeout=1), scope=scope)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    replacement = asyncio.create_task(
        service.generate(ImageRequest("four", timeout=1), scope=scope)
    )
    gate.set()
    assert await first
    assert await replacement
    await assets.close()


@pytest.mark.asyncio
async def test_expired_queue_deadline_never_calls_http(tmp_path, scope):
    gate = asyncio.Event()
    http = StubHttp(gate=gate)
    assets = AssetStore(tmp_path / "assets")
    service = ImageService(http, assets, max_running=1, max_pending=1)
    first = asyncio.create_task(service.generate(ImageRequest("one", timeout=1), scope=scope))
    await http.called.wait()
    with pytest.raises(Busy):
        await service.generate(ImageRequest("queued", timeout=0.01), scope=scope)
    assert len(http.calls) == 1
    gate.set()
    await first
    await assets.close()


@pytest.mark.asyncio
async def test_allowlisted_url_output_is_downloaded_without_regeneration(tmp_path, scope):
    class Downloader:
        def __init__(self):
            self.calls = []

        async def fetch(self, url, *, deadline):
            self.calls.append(url)
            return png()

        async def close(self):
            pass

    downloader = Downloader()
    assets = AssetStore(tmp_path / "assets", media_downloader=downloader)
    http = StubHttp({"data": [{"url": "https://media.example/result.png"}]})
    try:
        result = await ImageService(http, assets).generate(ImageRequest("draw"), scope=scope)
        assert len(result) == 1
        assert downloader.calls == ["https://media.example/result.png"]
        assert len(http.calls) == 1
    finally:
        await assets.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["store", "download"])
@pytest.mark.parametrize("trigger", ["deadline", "cancel"])
async def test_partial_assets_survive_timeout_or_cancel(tmp_path, scope, mode, trigger):
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingStore(AssetStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        async def store_bytes(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                started.set()
                await release.wait()
            return await super().store_bytes(*args, **kwargs)

    class BlockingDownloader:
        def __init__(self):
            self.calls = 0

        async def fetch(self, url, *, deadline):
            self.calls += 1
            if self.calls == 2:
                started.set()
                await release.wait()
            return png()

        async def close(self):
            release.set()

    downloader = BlockingDownloader() if mode == "download" else None
    assets = BlockingStore(tmp_path / "assets", media_downloader=downloader)
    encoded = base64.b64encode(png()).decode()
    data = (
        [{"url": "https://media.example/one"}, {"url": "https://media.example/two"}]
        if mode == "download"
        else [{"b64_json": encoded}, {"b64_json": encoded}]
    )
    http = StubHttp({"id": "req-upstream", "operation_id": "op-upstream", "data": data})
    timeout = 0.03 if trigger == "deadline" else 1
    task = asyncio.create_task(
        ImageService(http, assets).generate(ImageRequest("draw", n=2, timeout=timeout), scope=scope)
    )
    await started.wait()
    if trigger == "cancel":
        task.cancel()
    with pytest.raises(OutcomeUnknown) as error:
        await task
    assert error.value.partial is True
    assert error.value.request_id == "req-upstream"
    assert error.value.operation_id == "op-upstream"
    assert len(error.value.assets) == 1
    assert await assets.resolve(error.value.assets[0].asset_id, scope=scope)
    assert len(http.calls) == 1
    release.set()
    await assets.close()
