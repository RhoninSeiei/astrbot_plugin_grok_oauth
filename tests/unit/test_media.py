import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import grok_oauth.media as media_module
from grok_oauth.errors import AssetNotFound, UnsafeMediaSource
from grok_oauth.media import AssetStore
from grok_oauth.models import AssetScope


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (32, 16)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (20, 40, 60)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_jpeg_output_keeps_real_format_and_no_raw_base64(tmp_path):
    scope = AssetScope("qq-instance-a", "group:alpha", "conversation-a")
    assets = AssetStore(tmp_path / "assets")
    try:
        image = await assets.store_bytes(
            image_bytes("JPEG"), scope=scope, request_id="request-test"
        )
        assert image.mime_type == "image/jpeg"
        assert image.path.endswith((".jpg", ".jpeg"))
        assert (image.width, image.height) == (32, 16)
        assert image.asset_id
        assert "b64_json" not in (image.raw or {})
        assert "result" not in (image.raw or {})
        assert (await assets.resolve(image.asset_id, scope=scope)).is_file()
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_scope_isolation_hides_asset_existence(tmp_path):
    owner = AssetScope("qq", "user:7", "conversation-a")
    other = AssetScope("qq", "user:7", "conversation-b")
    assets = AssetStore(tmp_path / "assets")
    try:
        image = await assets.store_bytes(image_bytes(), scope=owner, request_id="req")
        with pytest.raises(AssetNotFound):
            await assets.resolve(image.asset_id, scope=other)
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_metadata_survives_store_reopen(tmp_path):
    scope = AssetScope("qq", "user:7", "conversation-a")
    root = tmp_path / "assets"
    first = AssetStore(root)
    image = await first.store_bytes(image_bytes(), scope=scope, request_id="req")
    await first.close()
    reopened = AssetStore(root)
    try:
        assert (await reopened.resolve(image.asset_id, scope=scope)).read_bytes() == image_bytes()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_import_reference_validates_data_uri_and_local_root(tmp_path):
    scope = AssetScope("qq", "user:7", "conversation-a")
    allowed = tmp_path / "incoming"
    allowed.mkdir()
    local = allowed / "misleading.txt"
    local.write_bytes(image_bytes("WEBP"))
    data_uri = "data:image/png;base64," + base64.b64encode(image_bytes()).decode()
    assets = AssetStore(tmp_path / "assets")
    try:
        local_id = await assets.import_reference(local, scope=scope, allowed_roots=(allowed,))
        uri_id = await assets.import_reference(data_uri, scope=scope, allowed_roots=())
        assert (await assets.resolve(local_id, scope=scope)).suffix == ".webp"
        assert (await assets.resolve(uri_id, scope=scope)).suffix == ".png"
        assert (await assets.to_data_uri(uri_id, scope=scope)).startswith("data:image/png;base64,")
        with pytest.raises(UnsafeMediaSource):
            await assets.import_reference(
                "https://example.com/image.png", scope=scope, allowed_roots=()
            )
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_symlink_cannot_escape_allowed_root(tmp_path):
    scope = AssetScope("qq", "user:7", "conversation-a")
    allowed = tmp_path / "incoming"
    allowed.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes())
    link = allowed / "reference.png"
    link.symlink_to(outside)
    assets = AssetStore(tmp_path / "assets")
    try:
        with pytest.raises(UnsafeMediaSource):
            await assets.import_reference(link, scope=scope, allowed_roots=(allowed,))
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_local_file_replaced_after_resolve_cannot_escape_root(tmp_path, monkeypatch):
    scope = AssetScope("qq", "user:7", "conversation-a")
    allowed = tmp_path / "incoming"
    allowed.mkdir()
    candidate = allowed / "reference.png"
    candidate.write_bytes(image_bytes())
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes("JPEG"))
    original_open = media_module.os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == allowed.resolve():
            replaced = True
            candidate.unlink()
            candidate.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(media_module.os, "open", replace_before_open)
    assets = AssetStore(tmp_path / "assets")
    try:
        with pytest.raises(UnsafeMediaSource):
            await assets.import_reference(candidate, scope=scope, allowed_roots=(allowed,))
        assert replaced
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_allowlisted_url_import_uses_credential_free_downloader(tmp_path):
    class Downloader:
        def __init__(self):
            self.calls = []

        async def fetch(self, url, *, deadline):
            self.calls.append((url, deadline))
            return image_bytes("PNG")

        async def close(self):
            pass

    scope = AssetScope("qq", "u", "c")
    downloader = Downloader()
    assets = AssetStore(tmp_path / "assets", media_downloader=downloader)
    try:
        asset_id = await assets.import_reference(
            "https://media.example/a.png", scope=scope, allowed_roots=()
        )
        assert (await assets.resolve(asset_id, scope=scope)).is_file()
        assert downloader.calls[0][0] == "https://media.example/a.png"
    finally:
        await assets.close()
