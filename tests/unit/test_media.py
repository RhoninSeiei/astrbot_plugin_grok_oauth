import base64
import os
from io import BytesIO
from pathlib import Path, PureWindowsPath

import pytest
from PIL import Image

import grok_oauth.media as media_module
from grok_oauth import windows_files
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
async def test_link_cannot_escape_allowed_root(tmp_path):
    scope = AssetScope("qq", "user:7", "conversation-a")
    allowed = tmp_path / "incoming"
    allowed.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes())
    link = allowed / "reference.png"
    if os.name == "nt":
        os.link(outside, link)
    else:
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
    if os.name == "nt":
        original_open_handle = windows_files._open_handle
        rename_blocked = False

        def rename_before_child_open(path, **kwargs):
            nonlocal rename_blocked
            if path == PureWindowsPath(candidate) and not kwargs["directory"]:
                with pytest.raises(PermissionError):
                    allowed.rename(tmp_path / "moved")
                rename_blocked = True
            return original_open_handle(path, **kwargs)

        monkeypatch.setattr(windows_files, "_open_handle", rename_before_child_open)
        assets = AssetStore(tmp_path / "assets")
        try:
            asset_id = await assets.import_reference(
                candidate, scope=scope, allowed_roots=(allowed,)
            )
            assert rename_blocked
            assert (await assets.resolve(asset_id, scope=scope)).read_bytes() == image_bytes()
        finally:
            await assets.close()
        return
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


def animated_gif_bytes():
    output = BytesIO()
    first = Image.new("RGB", (32, 16), "red")
    second = Image.new("RGB", (32, 16), "blue")
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    return output.getvalue()


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF", "AVIF"])
async def test_raster_reference_is_transcoded_to_png(tmp_path, fmt):
    scope = AssetScope("qq", "u", "c")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "qq-cache.bin"
    source.write_bytes(image_bytes(fmt, (32, 32)))
    with Image.open(source) as original:
        expected_pixel = original.convert("RGB").getpixel((0, 0))
    assets = AssetStore(tmp_path / "assets")
    try:
        asset_id = await assets.import_reference(source, scope=scope, allowed_roots=(incoming,))
        path = await assets.resolve(asset_id, scope=scope)
        assert path.suffix == ".png"
        with Image.open(path) as image:
            assert image.format == "PNG" and image.size == (32, 32)
            assert image.convert("RGB").getpixel((0, 0)) == expected_pixel
        assert (await assets.to_data_uri(asset_id, scope=scope)).startswith(
            "data:image/png;base64,"
        )
    finally:
        await assets.close()


async def test_gif_selects_first_frame_without_scanning_animation(tmp_path, monkeypatch):
    from PIL import GifImagePlugin

    scope = AssetScope("qq", "u", "c")
    source = animated_gif_bytes()
    original_seek = GifImagePlugin.GifImageFile.seek
    seen = []

    def first_frame_only(image, frame):
        seen.append(frame)
        assert frame == 0, "Normalization must not decode other animation frames"
        return original_seek(image, frame)

    monkeypatch.setattr(GifImagePlugin.GifImageFile, "seek", first_frame_only)
    assets = AssetStore(tmp_path / "assets")
    try:
        uri = "data:image/gif;base64," + base64.b64encode(source).decode()
        asset_id = await assets.import_reference(uri, scope=scope)
        with Image.open(await assets.resolve(asset_id, scope=scope)) as image:
            assert image.format == "PNG" and not getattr(image, "is_animated", False)
            assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
        assert set(seen).issubset({0})
    finally:
        await assets.close()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
async def test_native_formats_retain_original_bytes(tmp_path, fmt):
    data = image_bytes(fmt)
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    try:
        image = await assets.store_bytes(data, scope=scope, request_id="native")
        assert (await assets.resolve(image.asset_id, scope=scope)).read_bytes() == data
    finally:
        await assets.close()


async def test_gif_declared_mime_is_checked_before_conversion(tmp_path):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    try:
        invalid = "data:image/png;base64," + base64.b64encode(animated_gif_bytes()).decode()
        with pytest.raises(UnsafeMediaSource, match="Declared media type"):
            await assets.import_reference(invalid, scope=scope)
        assert list(assets._files.iterdir()) == []
        assert assets._assets == {}
    finally:
        await assets.close()


@pytest.mark.parametrize("data", [b"GIF89a", animated_gif_bytes()[:40], b"not an image"])
async def test_damaged_raster_is_not_persisted(tmp_path, data):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    try:
        with pytest.raises(UnsafeMediaSource):
            await assets.store_bytes(data, scope=scope, request_id="damaged")
        assert list(assets._files.iterdir()) == []
        assert assets._assets == {}
    finally:
        await assets.close()


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF", "AVIF"])
@pytest.mark.parametrize("limit", ["max_pixels", "max_decoded_bytes", "max_image_bytes"])
async def test_raster_limits_reject_before_decode(tmp_path, monkeypatch, limit, fmt):
    from grok_oauth.errors import ImageTooLarge

    scope = AssetScope("qq", "u", "c")
    data = image_bytes(fmt, (32, 32))
    values = {
        "max_pixels": 100,
        "max_decoded_bytes": 32 * 32 * 4 - 1,
        "max_image_bytes": len(data) - 1,
    }
    assets = AssetStore(tmp_path / "assets", **{limit: values[limit]})
    with Image.open(BytesIO(data)) as image:
        decoder_type = type(image)

    def no_source_decode(image, *args, **kwargs):
        raise AssertionError("Oversized images must fail before loading pixels")

    monkeypatch.setattr(decoder_type, "load", no_source_decode)
    try:
        with pytest.raises(ImageTooLarge):
            await assets.store_bytes(data, scope=scope, request_id="oversized")
        assert list(assets._files.iterdir()) == []
    finally:
        await assets.close()


async def test_raster_decompression_bomb_is_rejected(tmp_path, monkeypatch):
    scope = AssetScope("qq", "u", "c")
    data = image_bytes("GIF", (32, 32))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    assets = AssetStore(tmp_path / "assets")
    try:
        with pytest.raises(UnsafeMediaSource):
            await assets.store_bytes(data, scope=scope, request_id="bomb")
        assert list(assets._files.iterdir()) == []
    finally:
        await assets.close()


async def test_transcoding_preserves_palette_transparency(tmp_path):
    scope = AssetScope("qq", "u", "c")
    source = BytesIO()
    image = Image.new("P", (8, 8), 0)
    image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    image.save(source, format="GIF", transparency=0)
    assets = AssetStore(tmp_path / "assets")
    try:
        stored = await assets.store_bytes(source.getvalue(), scope=scope, request_id="alpha")
        with Image.open(await assets.resolve(stored.asset_id, scope=scope)) as decoded:
            assert decoded.mode == "RGBA"
            assert decoded.getpixel((0, 0)) == (255, 0, 0, 0)
    finally:
        await assets.close()


@pytest.mark.parametrize("limit_name", ["max_image_bytes", "max_total_bytes"])
async def test_transcoded_png_output_is_bounded_before_persistence(
    tmp_path, monkeypatch, limit_name
):
    from grok_oauth.errors import ImageTooLarge

    scope = AssetScope("qq", "u", "c")
    source = image_bytes("GIF")
    limit = len(source) + 50
    assets = AssetStore(tmp_path / "assets", **{limit_name: limit})
    original_save = Image.Image.save
    stopped = []

    def oversized_png(image, output, format=None, **kwargs):
        if format == "PNG":
            output.write(b"x" * limit)
            with pytest.raises(ImageTooLarge):
                output.write(b"overflow")
            assert output.tell() == limit
            stopped.append(True)
            # The real encoder must abort the store on overflow.
            output.write(b"overflow")
        return original_save(image, output, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", oversized_png)
    try:
        with pytest.raises(ImageTooLarge):
            await assets.store_bytes(source, scope=scope, request_id="output-limit")
        assert stopped == [True]
        assert list(assets._files.iterdir()) == []
        assert assets._assets == {}
    finally:
        await assets.close()


@pytest.mark.parametrize("limit_name", ["max_image_bytes", "max_total_bytes"])
async def test_real_high_entropy_raster_cannot_expand_past_byte_budget(tmp_path, limit_name):
    import random

    from grok_oauth.errors import ImageTooLarge

    scope = AssetScope("qq", "u", "c")
    image = Image.frombytes("P", (128, 128), random.Random(0).randbytes(128 * 128))
    image.putpalette([value for shade in range(256) for value in (shade, shade, shade)])
    source, normalized = BytesIO(), BytesIO()
    image.save(source, format="GIF")
    image.convert("RGB").save(normalized, format="PNG")
    assert len(normalized.getvalue()) > len(source.getvalue())
    assets = AssetStore(tmp_path / "assets", **{limit_name: len(source.getvalue())})
    try:
        with pytest.raises(ImageTooLarge, match="Converted image"):
            await assets.store_bytes(source.getvalue(), scope=scope, request_id="real-output-limit")
        assert list(assets._files.iterdir()) == []
        assert assets._assets == {}
    finally:
        await assets.close()


async def test_unsupported_ico_is_rejected_before_decoder_load(tmp_path, monkeypatch):
    from PIL import IcoImagePlugin

    scope = AssetScope("qq", "u", "c")
    source = image_bytes("ICO", (32, 32))
    calls = []

    def forbidden_load(image, *args, **kwargs):
        calls.append(True)
        raise AssertionError("ICO must not enter its eager decoder")

    monkeypatch.setattr(IcoImagePlugin.IcoImageFile, "load", forbidden_load)
    assets = AssetStore(tmp_path / "assets")
    try:
        with pytest.raises(UnsafeMediaSource):
            await assets.store_bytes(source, scope=scope, request_id="unsupported")
        assert calls == []
        assert list(assets._files.iterdir()) == []
    finally:
        await assets.close()


async def test_eps_never_enters_unapproved_decoder(tmp_path, monkeypatch):
    from PIL import EpsImagePlugin

    scope = AssetScope("qq", "u", "c")
    source = image_bytes("EPS")

    def forbidden_open(image):
        raise AssertionError("EPS must not enter its parser or external decoder")

    monkeypatch.setattr(EpsImagePlugin.EpsImageFile, "_open", forbidden_open)
    assets = AssetStore(tmp_path / "assets")
    try:
        with pytest.raises(UnsafeMediaSource):
            await assets.store_bytes(source, scope=scope, request_id="unsupported")
        assert list(assets._files.iterdir()) == []
    finally:
        await assets.close()


async def test_cancel_during_transcode_waits_for_worker_without_persisting(tmp_path, monkeypatch):
    import asyncio
    import threading

    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    started, release = threading.Event(), threading.Event()
    original_transcode = assets._transcode_raster

    def gated_transcode(data):
        started.set()
        assert release.wait(2)
        return original_transcode(data)

    monkeypatch.setattr(assets, "_transcode_raster", gated_transcode)
    task = asyncio.create_task(
        assets.store_bytes(animated_gif_bytes(), scope=scope, request_id="cancel")
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(assets._files.iterdir()) == []
    assert assets._assets == {}
    await assets.close()
