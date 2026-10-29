import asyncio
import base64
import ipaddress
import json
import time

import httpx
import pytest

from grok_oauth.errors import AssetExpired, AssetNotFound, MediaDownloadError, UnsafeMediaSource
from grok_oauth.models import AssetScope
from grok_oauth.video_download import ExternalVideoDownloader
from grok_oauth.video_store import VideoStore


def box(kind, content):
    return (len(content) + 8).to_bytes(4, "big") + kind + content


def movie(duration=6, *, track_duration=None, handler=b"vide", track=True):
    def header(seconds):
        return bytes(12) + (1000).to_bytes(4, "big") + int(seconds * 1000).to_bytes(4, "big")

    movie_header = box(b"mvhd", header(duration))
    track_duration = duration if track_duration is None else track_duration
    track_header = box(b"tkhd", bytes(20) + int(track_duration * 1000).to_bytes(4, "big"))
    media_header = box(b"mdhd", header(track_duration))
    media_handler = box(b"hdlr", bytes(8) + handler)
    tracks = (
        box(b"trak", track_header + box(b"mdia", media_header + media_handler)) if track else b""
    )
    return (
        box(b"ftyp", b"isom" + bytes(4) + b"mp42")
        + box(b"moov", movie_header + tracks)
        + box(b"mdat", bytes(24))
    )


def uri(content):
    return "data:video/mp4;base64," + base64.b64encode(content).decode("ascii")


@pytest.fixture
async def store(tmp_path):
    value = VideoStore(tmp_path / "videos")
    yield value
    await value.close()


SCOPE = AssetScope("qq", "group:1", "conversation")


@pytest.mark.asyncio
async def test_data_uri_import_scoped_bound_and_survives_restart(store):
    asset_id = await store.import_reference(uri(movie()), scope=SCOPE, binding="account-1")
    assert await store.read_bytes(asset_id, scope=SCOPE) == movie()
    assert await store.asset_binding(asset_id, scope=SCOPE) == "account-1"
    metadata = json.loads(store._metadata.read_text())
    assert metadata["assets"][asset_id]["provenance"] == "imported"
    assert "data:" not in store._metadata.read_text()
    other = AssetScope("qq", "group:2", "conversation")
    with pytest.raises(AssetNotFound):
        await store.read_bytes(asset_id, scope=other)
    reopened = VideoStore(store.root)
    try:
        assert await reopened.asset_binding(asset_id, scope=SCOPE) == "account-1"
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        movie(8.701),
        movie(6, track_duration=20),
        movie(0),
        movie(track=False),
        movie(handler=b"soun"),
        b"not an mp4",
        movie()[:-1],
    ],
)
async def test_import_rejects_oversized_timeline_and_invalid_video(store, content):
    with pytest.raises(UnsafeMediaSource):
        await store.import_reference(uri(content), scope=SCOPE, binding="account")
    assert not store._metadata.exists()


@pytest.mark.asyncio
async def test_edit_duration_boundary_accepted(store):
    asset_id = await store.import_reference(uri(movie(8.7)), scope=SCOPE, binding="account")
    assert (await store.get_asset(asset_id, scope=SCOPE)).duration == 8.7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "data:video/webm;base64,AAAA",
        "data:video/mp4;base64,@@@@",
        "data:video/mp4;base64,AA A=",
        "http://media.example/video",
        "https://user@media.example/video",
        "https://media.example/video#secret",
        "https://vidgen.x.ai/video",
    ],
)
async def test_only_canonical_data_and_explicit_source_hosts(store, reference):
    with pytest.raises(UnsafeMediaSource):
        await store.import_reference(reference, scope=SCOPE, binding="account")


@pytest.mark.asyncio
async def test_data_size_rejected_before_decoding(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "small", max_video_bytes=100)

    def forbidden(*args, **kwargs):
        raise AssertionError("Must bound encoded input before allocating decoded bytes")

    monkeypatch.setattr(base64, "b64decode", forbidden)
    try:
        with pytest.raises(UnsafeMediaSource):
            await store.import_reference(
                "data:video/mp4;base64," + "A" * 140, scope=SCOPE, binding="account"
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_local_import_only_explicit_root_and_no_symlinks(store, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "video.mp4"
    source.write_bytes(movie())
    with pytest.raises(UnsafeMediaSource):
        await store.import_reference(str(source), scope=SCOPE, binding="account")
    for reference in (str(source), source.as_uri()):
        asset_id = await store.import_reference(
            reference, scope=SCOPE, binding="account", allowed_roots=(root,)
        )
        assert await store.read_bytes(asset_id, scope=SCOPE) == movie()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(movie())
    link = root / "link.mp4"
    link.symlink_to(outside)
    linked_dir = root / "linked"
    linked_dir.symlink_to(tmp_path, target_is_directory=True)
    for reference in (
        str(outside),
        str(link),
        str(linked_dir / "outside.mp4"),
        str(root / ".." / "outside.mp4"),
        "file://localhost" + str(source),
        str(root),
    ):
        with pytest.raises(UnsafeMediaSource):
            await store.import_reference(
                reference, scope=SCOPE, binding="account", allowed_roots=(root,)
            )
    with pytest.raises(UnsafeMediaSource):
        await store.import_reference(
            str(source), scope=SCOPE, binding="account", allowed_roots=("/",)
        )


@pytest.mark.asyncio
async def test_expiry_and_lease(store):
    asset_id = await store.import_reference(uri(movie()), scope=SCOPE, binding="account")
    async with store.lease(asset_id, scope=SCOPE) as path:
        data = json.loads(store._metadata.read_text())
        data["assets"][asset_id]["expires"] = time.time() - 1
        store._write(data)
        await store.cleanup()
        assert path.exists()
        with pytest.raises(AssetExpired):
            await store.read_bytes(asset_id, scope=SCOPE)
    await store.cleanup()
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "198.18.0.1", "::1"]
)
async def test_source_dns_private_rejected(address):
    requests = []

    async def resolve(host, port):
        return [ipaddress.ip_address(address)]

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: requests.append(r) or httpx.Response(200, content=movie())
        )
    )
    downloader = ExternalVideoDownloader(["media.example"], client=client, resolver=resolve)
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://media.example/video", deadline=time.monotonic() + 2)
        assert not requests
    finally:
        await downloader.close()
    assert client.is_closed


@pytest.mark.asyncio
async def test_https_import_pinned_without_default_credentials(tmp_path):
    requests = []

    async def resolve(host, port):
        return [ipaddress.ip_address("8.8.8.8")]

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                requests.append(r)
                or httpx.Response(200, headers={"content-type": "video/mp4"}, content=movie())
            )
        ),
        headers={"authorization": "SECRET"},
        cookies={"session": "SECRET"},
        auth=("SECRET", "SECRET"),
        follow_redirects=True,
    )
    downloader = ExternalVideoDownloader(["media.example"], client=client, resolver=resolve)
    store = VideoStore(tmp_path / "videos", source_downloader=downloader)
    try:
        asset_id = await store.import_reference(
            "https://media.example/video?signature=private", scope=SCOPE, binding="account"
        )
        assert await store.read_bytes(asset_id, scope=SCOPE) == movie()
        assert requests[0].url.host == "8.8.8.8"
        assert requests[0].headers["host"] == "media.example"
        assert requests[0].extensions["sni_hostname"] == "media.example"
        assert "authorization" not in requests[0].headers
        assert "cookie" not in requests[0].headers
        assert "private" not in store._metadata.read_text()
    finally:
        await store.close()
    assert client.is_closed
    with pytest.raises(RuntimeError):
        await store.import_reference(uri(movie()), scope=SCOPE, binding="account")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/video",
        "https://127.0.0.1/video",
        "http://media.example/video",
        "https://media.example:8443/video",
    ],
)
async def test_redirect_destination_revalidated(location):
    requests = []

    async def resolve(host, port):
        return [ipaddress.ip_address("8.8.8.8")]

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: requests.append(r) or httpx.Response(302, headers={"location": location})
        )
    )
    downloader = ExternalVideoDownloader(["media.example"], client=client, resolver=resolve)
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://media.example/video", deadline=time.monotonic() + 2)
        assert len(requests) == 1
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_import_cancel_leaves_no_asset(tmp_path):
    class Sources:
        async def fetch(self, url, *, deadline):
            await asyncio.Event().wait()

        async def close(self):
            pass

    store = VideoStore(tmp_path / "videos", source_downloader=Sources())
    task = asyncio.create_task(
        store.import_reference("https://media.example/video", scope=SCOPE, binding="account")
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not store._metadata.exists()
    await store.close()


@pytest.mark.asyncio
async def test_fixed_qq_source_proxy_never_uses_result_origin_or_credentials():
    requests = []
    direct_requests = []

    async def resolve(host, port):
        return [ipaddress.ip_address("198.18.0.1")]

    proxy = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: requests.append(r) or httpx.Response(200, content=movie())
        ),
        auth=("SECRET", "SECRET"),
        headers={"authorization": "SECRET"},
        cookies={"session": "SECRET"},
        follow_redirects=True,
    )
    direct = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: direct_requests.append(r) or httpx.Response(200))
    )
    downloader = ExternalVideoDownloader(
        ["multimedia.nt.qq.com.cn", "vidgen.x.ai"],
        source_proxy="http://trusted.proxy",
        proxy_client=proxy,
        client=direct,
        resolver=resolve,
    )
    try:
        assert (
            await downloader.fetch(
                "https://multimedia.nt.qq.com.cn/video?signature=PRIVATE",
                deadline=time.monotonic() + 2,
            )
            == movie()
        )
        request = requests[0]
        assert request.url.host == "multimedia.nt.qq.com.cn"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        for target in (
            "https://vidgen.x.ai/video",
            "https://multimedia.nt.qq.com.cn.evil.example/video",
            "https://multimedia.nt.qq.com.cn./video",
            "http://multimedia.nt.qq.com.cn/video",
            "https://user@multimedia.nt.qq.com.cn/video",
        ):
            with pytest.raises(UnsafeMediaSource):
                await downloader.fetch(target, deadline=time.monotonic() + 2)
        assert len(requests) == 1 and not direct_requests
    finally:
        await downloader.close()
    assert proxy.is_closed and direct.is_closed


@pytest.mark.asyncio
async def test_qq_proxy_rejects_all_redirects_without_second_request():
    requests = []
    proxy = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                requests.append(r)
                or httpx.Response(
                    302, headers={"location": "https://multimedia.nt.qq.com.cn/other"}
                )
            )
        ),
        follow_redirects=True,
    )
    downloader = ExternalVideoDownloader(
        ["multimedia.nt.qq.com.cn"], source_proxy="http://trusted.proxy", proxy_client=proxy
    )
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch(
                "https://multimedia.nt.qq.com.cn/video", deadline=time.monotonic() + 2
            )
        assert len(requests) == 1
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_public_source_ignores_injected_auto_redirect_setting():
    requests = []

    async def resolve(host, port):
        return [ipaddress.ip_address("8.8.8.8")]

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                requests.append(r)
                or httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
            )
        ),
        follow_redirects=True,
    )
    downloader = ExternalVideoDownloader(["media.example"], client=client, resolver=resolve)
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://media.example/video", deadline=time.monotonic() + 2)
        assert len(requests) == 1
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_stream_and_declared_size_limits():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123456"
            yield b"789012"

    async def resolve(host, port):
        return [ipaddress.ip_address("8.8.8.8")]

    for response in (
        httpx.Response(200, headers={"content-length": "11"}, content=b"123"),
        httpx.Response(200, stream=Stream()),
    ):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response))
        downloader = ExternalVideoDownloader(
            ["media.example"], max_bytes=10, client=client, resolver=resolve
        )
        try:
            with pytest.raises(UnsafeMediaSource):
                await downloader.fetch("https://media.example/video", deadline=time.monotonic() + 2)
        finally:
            await downloader.close()
        assert client.is_closed


@pytest.mark.asyncio
async def test_exact_decoded_length_checked_before_allocation(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "small", max_video_bytes=100)

    def forbidden(*args, **kwargs):
        raise AssertionError("Decoded bound must precede allocation")

    monkeypatch.setattr(base64, "b64decode", forbidden)
    try:
        with pytest.raises(UnsafeMediaSource):
            await store.import_reference(
                "data:video/mp4;base64," + "A" * 136, scope=SCOPE, binding="account"
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_invalid_deadlines_and_missing_binding_fail_without_storage(store):
    for deadline in (True, float("nan"), float("inf"), "120"):
        with pytest.raises(TypeError):
            await store.import_reference(
                uri(movie()), scope=SCOPE, binding="account", deadline=deadline
            )
    with pytest.raises(MediaDownloadError):
        await store.import_reference(
            uri(movie()), scope=SCOPE, binding="account", deadline=time.monotonic() - 1
        )
    with pytest.raises(UnsafeMediaSource):
        await store.import_reference(uri(movie()), scope=SCOPE, binding="")
    assert not store._metadata.exists()


@pytest.mark.asyncio
async def test_source_client_closes_when_result_client_close_fails(tmp_path):
    class BrokenResults:
        async def close(self):
            raise RuntimeError("test close failure")

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    store = VideoStore(
        tmp_path / "videos",
        media_downloader=BrokenResults(),
        source_downloader=ExternalVideoDownloader(client=client),
    )
    with pytest.raises(RuntimeError):
        await store.close()
    assert client.is_closed


@pytest.mark.asyncio
async def test_mixed_dns_public_private_rejected():
    requests = []

    async def resolve(host, port):
        return [ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("127.0.0.1")]

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200))
    )
    downloader = ExternalVideoDownloader(["media.example"], client=client, resolver=resolve)
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://media.example/video", deadline=time.monotonic() + 2)
        assert not requests
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_duplicate_movie_headers_rejected(store):
    content = movie() + box(
        b"moov", box(b"mvhd", bytes(12) + (1000).to_bytes(4, "big") + (6000).to_bytes(4, "big"))
    )
    with pytest.raises(UnsafeMediaSource):
        await store.import_reference(uri(content), scope=SCOPE, binding="account")


@pytest.mark.asyncio
async def test_source_import_deadline_leaves_no_asset(tmp_path):
    class Source:
        async def fetch(self, url, *, deadline):
            await asyncio.Event().wait()

        async def close(self):
            pass

    store = VideoStore(tmp_path / "videos", source_downloader=Source())
    try:
        with pytest.raises(MediaDownloadError):
            await store.import_reference(
                "https://media.example/video",
                scope=SCOPE,
                binding="account",
                deadline=time.monotonic() + 0.01,
            )
        assert not store._metadata.exists()
    finally:
        await store.close()
