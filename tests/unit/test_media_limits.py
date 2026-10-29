import asyncio
import json
import threading
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import grok_oauth.media as media_module
from grok_oauth.errors import AssetExpired, ImageTooLarge, UnsafeMediaSource
from grok_oauth.media import AssetStore
from grok_oauth.models import AssetScope


def png(size=(32, 16)):
    output = BytesIO()
    Image.new("RGB", size).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_rejects_bad_image_and_boolean_limits(tmp_path):
    scope = AssetScope("qq", "u", "c")
    with pytest.raises(TypeError):
        AssetStore(tmp_path / "bad", ttl_seconds=True)
    assets = AssetStore(tmp_path / "assets")
    try:
        with pytest.raises(UnsafeMediaSource):
            await assets.store_bytes(b"not a jpeg", scope=scope, request_id="req")
        with pytest.raises(ImageTooLarge):
            await assets.store_bytes(b"x" * (20 * 1024 * 1024 + 1), scope=scope, request_id="req")
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_rejects_compressed_image_over_pixel_budget(tmp_path):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets", max_pixels=100)
    try:
        with pytest.raises(ImageTooLarge):
            await assets.store_bytes(png((11, 10)), scope=scope, request_id="req")
    finally:
        await assets.close()


@pytest.mark.asyncio
async def test_expiration_persists_and_lease_protects_cleanup(tmp_path):
    scope = AssetScope("qq", "u", "c")
    root = tmp_path / "assets"
    assets = AssetStore(root, ttl_seconds=0.02)
    image = await assets.store_bytes(png(), scope=scope, request_id="req")
    async with assets.lease(image.asset_id, scope=scope) as path:
        await asyncio.sleep(0.03)
        await assets.cleanup()
        assert path.exists()
    with pytest.raises(AssetExpired):
        await assets.resolve(image.asset_id, scope=scope)
    await assets.cleanup()
    await assets.close()

    metadata = json.loads((root / "metadata.json").read_text())
    assert image.asset_id not in metadata["assets"]


def test_constructor_rejects_non_finite_or_boolean_limits(tmp_path):
    for value in (True, 0, -1, float("inf")):
        with pytest.raises((TypeError, ValueError)):
            AssetStore(tmp_path / str(value), ttl_seconds=value)


@pytest.mark.asyncio
async def test_cancel_during_asset_replace_waits_and_rolls_back(tmp_path, monkeypatch):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    started = threading.Event()
    release = threading.Event()
    original_replace = media_module.os.replace

    def gated_replace(source, target):
        if Path(target).parent == assets._files:
            started.set()
            assert release.wait(2)
        return original_replace(source, target)

    monkeypatch.setattr(media_module.os, "replace", gated_replace)
    task = asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id="req"))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(assets._files.iterdir()) == []
    assert json.loads((assets.root / "metadata.json").read_text())["assets"] == {}
    await assets.close()


@pytest.mark.asyncio
async def test_cancel_during_metadata_fsync_keeps_lock_and_rolls_back(tmp_path, monkeypatch):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    started = threading.Event()
    release = threading.Event()
    original_persist = assets._persist_metadata
    calls = 0

    def gated_persist():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(2)
        original_persist()

    monkeypatch.setattr(assets, "_persist_metadata", gated_persist)
    task = asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id="req"))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
        assert assets._lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(assets._files.iterdir()) == []
    assert json.loads((assets.root / "metadata.json").read_text())["assets"] == {}
    await assets.close()


@pytest.mark.asyncio
async def test_asset_worker_limit_is_held_until_cancelled_thread_finishes(tmp_path, monkeypatch):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets", max_workers=2)
    original_inspect = assets._inspect_image
    release = threading.Event()
    two_started = threading.Event()
    state_lock = threading.Lock()
    active = 0
    maximum = 0
    entered = 0

    def gated_inspect(data):
        nonlocal active, maximum, entered
        with state_lock:
            active += 1
            entered += 1
            maximum = max(maximum, active)
            if entered == 2:
                two_started.set()
        assert release.wait(2)
        try:
            return original_inspect(data)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(assets, "_inspect_image", gated_inspect)
    tasks = [
        asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id=str(index)))
        for index in range(3)
    ]
    assert await asyncio.to_thread(two_started.wait, 1)
    tasks[0].cancel()
    await asyncio.sleep(0)
    assert entered == 2
    assert not tasks[0].done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    assert len(await asyncio.gather(*tasks[1:])) == 2
    assert maximum == 2
    await assets.close()


@pytest.mark.asyncio
async def test_cancelled_worker_waiter_never_starts_thread(tmp_path, monkeypatch):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets", max_workers=2)
    original_inspect = assets._inspect_image
    release = threading.Event()
    two_started = threading.Event()
    state_lock = threading.Lock()
    entered = 0

    def gated_inspect(data):
        nonlocal entered
        with state_lock:
            entered += 1
            if entered == 2:
                two_started.set()
        assert release.wait(2)
        return original_inspect(data)

    monkeypatch.setattr(assets, "_inspect_image", gated_inspect)
    running = [
        asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id=str(index)))
        for index in range(2)
    ]
    assert await asyncio.to_thread(two_started.wait, 1)
    waiting = asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id="waiting"))
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.sleep(0)
    cancelled_without_releasing_workers = waiting.done()
    release.set()
    await asyncio.gather(*running)
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert cancelled_without_releasing_workers
    assert entered == 2
    await assets.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inspect", "write"])
async def test_close_waits_for_active_store_before_returning(tmp_path, monkeypatch, stage):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    started = threading.Event()
    release = threading.Event()
    if stage == "inspect":
        original = assets._inspect_image

        def gated(data):
            started.set()
            assert release.wait(2)
            return original(data)

        monkeypatch.setattr(assets, "_inspect_image", gated)
    else:
        original = media_module.os.replace

        def gated(source, target):
            if Path(target).parent == assets._files:
                started.set()
                assert release.wait(2)
            return original(source, target)

        monkeypatch.setattr(media_module.os, "replace", gated)

    storing = asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id="req"))
    assert await asyncio.to_thread(started.wait, 1)
    closing = asyncio.create_task(assets.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    image = await storing
    await closing
    metadata = json.loads((assets.root / "metadata.json").read_text())["assets"]
    assert image.asset_id in metadata
    with pytest.raises(RuntimeError):
        await assets.store_bytes(png(), scope=scope, request_id="late")


@pytest.mark.asyncio
async def test_cancelled_close_can_be_resumed_after_active_store(tmp_path, monkeypatch):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    started = threading.Event()
    release = threading.Event()
    original = assets._inspect_image

    def gated(data):
        started.set()
        assert release.wait(2)
        return original(data)

    monkeypatch.setattr(assets, "_inspect_image", gated)
    storing = asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id="req"))
    assert await asyncio.to_thread(started.wait, 1)
    closing = asyncio.create_task(assets.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(RuntimeError):
        await assets.store_bytes(png(), scope=scope, request_id="late")
    release.set()
    await storing
    await assets.close()


@pytest.mark.asyncio
async def test_second_cancel_cannot_interrupt_queued_rollback(tmp_path, monkeypatch):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets", max_workers=1)
    write_started = threading.Event()
    release_write = threading.Event()
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    original_replace = media_module.os.replace

    def gated_replace(source, target):
        if Path(target).parent == assets._files:
            write_started.set()
            assert release_write.wait(2)
        return original_replace(source, target)

    def occupy_worker():
        blocker_started.set()
        assert release_blocker.wait(2)

    monkeypatch.setattr(media_module.os, "replace", gated_replace)
    storing = asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id="req"))
    assert await asyncio.to_thread(write_started.wait, 1)
    blocker = asyncio.create_task(assets._thread(occupy_worker))
    storing.cancel()
    release_write.set()
    assert await asyncio.to_thread(blocker_started.wait, 1)
    storing.cancel()
    await asyncio.sleep(0)
    still_waiting_for_rollback = not storing.done()
    release_blocker.set()
    await blocker
    with pytest.raises(asyncio.CancelledError):
        await storing
    assert still_waiting_for_rollback
    assert list(assets._files.iterdir()) == []
    assert json.loads((assets.root / "metadata.json").read_text())["assets"] == {}
    await assets.close()


@pytest.mark.asyncio
async def test_second_cancel_cannot_interrupt_rollback_waiting_for_metadata_lock(
    tmp_path, monkeypatch
):
    scope = AssetScope("qq", "u", "c")
    assets = AssetStore(tmp_path / "assets")
    persist_started = threading.Event()
    release_persist = threading.Event()
    holder_acquired = asyncio.Event()
    release_holder = asyncio.Event()
    original_persist = assets._persist_metadata
    calls = 0

    def gated_persist():
        nonlocal calls
        calls += 1
        if calls == 1:
            persist_started.set()
            assert release_persist.wait(2)
        original_persist()

    async def hold_metadata_lock():
        async with assets._lock:
            holder_acquired.set()
            await release_holder.wait()

    monkeypatch.setattr(assets, "_persist_metadata", gated_persist)
    storing = asyncio.create_task(assets.store_bytes(png(), scope=scope, request_id="req"))
    assert await asyncio.to_thread(persist_started.wait, 1)
    holder = asyncio.create_task(hold_metadata_lock())
    storing.cancel()
    release_persist.set()
    await holder_acquired.wait()
    storing.cancel()
    await asyncio.sleep(0)
    still_waiting_for_rollback = not storing.done()
    release_holder.set()
    await holder
    with pytest.raises(asyncio.CancelledError):
        await storing
    assert still_waiting_for_rollback
    assert list(assets._files.iterdir()) == []
    assert json.loads((assets.root / "metadata.json").read_text())["assets"] == {}
    await assets.close()
