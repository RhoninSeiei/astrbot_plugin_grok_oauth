import asyncio
import ipaddress
import time

import httpx
import pytest

from grok_oauth.errors import MediaDownloadError, UnsafeMediaSource
from grok_oauth.media_download import PublicMediaDownloader
from grok_oauth.video_download import CredentialFreeVideoDownloader


class ForbiddenDirect:
    async def fetch(self, url, *, deadline):
        raise AssertionError("Fixed-origin proxy download must not consult local DNS")

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_fixed_origin_proxy_avoids_fake_dns_and_does_not_inherit_credentials():
    requests = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                requests.append(r)
                or httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video")
            )
        ),
        headers={"authorization": "Bearer API_SENTINEL", "x-secret": "PRIVATE"},
        cookies={"session": "COOKIE_SENTINEL"},
        auth=("private", "password"),
        follow_redirects=True,
    )
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai"],
        result_proxy="http://trusted.proxy:8080",
        proxy_client=client,
        public_downloader=ForbiddenDirect(),
    )
    try:
        assert (
            await downloader.fetch(
                "https://vidgen.x.ai:443/result.mp4?signature=SIGNED", deadline=time.monotonic() + 5
            )
            == b"video"
        )
        request = requests[0]
        assert request.url.host == "vidgen.x.ai"
        assert request.headers["host"] == "vidgen.x.ai"
        assert request.headers["accept"] == "video/mp4,application/octet-stream"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "x-secret" not in request.headers
    finally:
        await downloader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "http://vidgen.x.ai/result",
        "https://user@vidgen.x.ai/result",
        "https://user:password@vidgen.x.ai/result",
        "https://vidgen.x.ai:8443/result",
        "https://vidgen.x.ai/result#fragment",
        "https://vidgen.x.ai.evil.example/result",
        "https://vidgen.x.ai./result",
        "https://127.0.0.1/result",
        "https://169.254.169.254/result",
        "https://evil.example@127.0.0.1/result",
        "https://vidgen.x.ai/\nresult",
    ],
)
async def test_proxy_never_reaches_deceptive_or_non_fixed_urls(target):
    requests = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: requests.append(r) or httpx.Response(200, content=b"video")
        )
    )

    async def resolve(host, port):
        return [ipaddress.ip_address("127.0.0.1")]

    public = PublicMediaDownloader(["vidgen.x.ai"], resolver=resolve)
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai"],
        result_proxy="http://trusted.proxy",
        proxy_client=client,
        public_downloader=public,
    )
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch(target, deadline=time.monotonic() + 5)
        assert not requests
    finally:
        await downloader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    ["https://127.0.0.1/private", "https://evil.example/private", "https://vidgen.x.ai/other"],
)
async def test_all_proxy_redirects_rejected_without_follow_up(location):
    requests = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: requests.append(r) or httpx.Response(302, headers={"location": location})
        ),
        follow_redirects=True,
    )
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai"],
        result_proxy="http://trusted.proxy",
        proxy_client=client,
        public_downloader=ForbiddenDirect(),
    )
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://vidgen.x.ai/result", deadline=time.monotonic() + 5)
        assert len(requests) == 1 and requests[0].url.host == "vidgen.x.ai"
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_other_admin_hosts_keep_direct_dns_pinning_and_reject_private_dns():
    proxy_requests = []
    direct_requests = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: proxy_requests.append(r) or httpx.Response(200))
    )
    direct_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                direct_requests.append(r)
                or httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video")
            )
        )
    )
    address = "127.0.0.1"

    async def resolve(host, port):
        assert host == "media.example"
        return [ipaddress.ip_address(address)]

    public = PublicMediaDownloader(
        ["media.example"],
        client=direct_client,
        resolver=resolve,
        allowed_content_types=("video/mp4", "application/octet-stream"),
    )
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai", "media.example"],
        result_proxy="http://trusted.proxy",
        proxy_client=client,
        public_downloader=public,
    )
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://media.example/result", deadline=time.monotonic() + 5)
        assert not proxy_requests and not direct_requests
        address = "8.8.8.8"
        assert (
            await downloader.fetch("https://media.example/result", deadline=time.monotonic() + 5)
            == b"video"
        )
        assert len(direct_requests) == 1 and direct_requests[0].url.host == "8.8.8.8"
        assert not proxy_requests
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_no_proxy_retains_fixed_host_private_dns_rejection():
    async def resolve(host, port):
        return [ipaddress.ip_address("198.18.0.1")]

    public = PublicMediaDownloader(["vidgen.x.ai"], resolver=resolve)
    downloader = CredentialFreeVideoDownloader(["vidgen.x.ai"], public_downloader=public)
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://vidgen.x.ai/result", deadline=time.monotonic() + 5)
    finally:
        await downloader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,error",
    [
        (
            httpx.Response(200, headers={"content-type": "text/html"}, content=b"video"),
            UnsafeMediaSource,
        ),
        (
            httpx.Response(200, headers={"content-length": "20"}, content=b"video"),
            UnsafeMediaSource,
        ),
        (
            httpx.Response(200, headers={"content-length": "-1"}, content=b"video"),
            MediaDownloadError,
        ),
        (httpx.Response(403, content=b"PRIVATE_UPSTREAM"), MediaDownloadError),
    ],
)
async def test_proxy_bounds_and_status_errors(response, error):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response))
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai"],
        max_bytes=10,
        result_proxy="http://trusted.proxy",
        proxy_client=client,
        public_downloader=ForbiddenDirect(),
    )
    try:
        with pytest.raises(error) as captured:
            await downloader.fetch(
                "https://vidgen.x.ai/result?SIGNED_SECRET", deadline=time.monotonic() + 5
            )
        assert "PRIVATE" not in str(captured.value) and "SIGNED_SECRET" not in str(captured.value)
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_proxy_streaming_size_and_deadline_bounds():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123456"
            yield b"789012"

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream()))
    )
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai"],
        max_bytes=10,
        result_proxy="http://trusted.proxy",
        proxy_client=client,
        public_downloader=ForbiddenDirect(),
    )
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://vidgen.x.ai/result", deadline=time.monotonic() + 5)
    finally:
        await downloader.close()

    async def delayed(request):
        await asyncio.sleep(1)
        return httpx.Response(200, content=b"video")

    client = httpx.AsyncClient(transport=httpx.MockTransport(delayed))
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai"],
        result_proxy="http://trusted.proxy",
        proxy_client=client,
        public_downloader=ForbiddenDirect(),
    )
    try:
        with pytest.raises(MediaDownloadError) as captured:
            await downloader.fetch(
                "https://vidgen.x.ai/result?PRIVATE", deadline=time.monotonic() + 0.01
            )
        assert "PRIVATE" not in str(captured.value)
    finally:
        await downloader.close()


def test_proxy_client_defaults_keep_tls_validation_and_ignore_environment(monkeypatch):
    import grok_oauth.video_download as module

    arguments = []
    real_client_class = httpx.AsyncClient

    class Client:
        def __init__(self, **kwargs):
            arguments.append(kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai"], result_proxy="http://trusted.proxy", public_downloader=ForbiddenDirect()
    )
    assert arguments == [
        {
            "proxy": "http://trusted.proxy",
            "trust_env": False,
            "follow_redirects": False,
            "timeout": None,
        }
    ]
    assert isinstance(downloader._proxy, Client)
    assert real_client_class.__init__.__kwdefaults__["verify"] is True


@pytest.mark.asyncio
async def test_proxy_closes_even_when_direct_downloader_close_fails():
    class BrokenDirect(ForbiddenDirect):
        async def close(self):
            raise RuntimeError("close failed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    downloader = CredentialFreeVideoDownloader(
        ["vidgen.x.ai"],
        result_proxy="http://trusted.proxy",
        proxy_client=client,
        public_downloader=BrokenDirect(),
    )
    with pytest.raises(RuntimeError):
        await downloader.close()
    assert client.is_closed
