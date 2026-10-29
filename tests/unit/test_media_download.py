import ipaddress
import time

import httpx
import pytest

import grok_oauth.media_download as media_download_module
from grok_oauth.errors import UnsafeMediaSource
from grok_oauth.media_download import PublicMediaDownloader


class Resolver:
    def __init__(self, answers):
        self.answers = answers

    async def __call__(self, host, port):
        return [ipaddress.ip_address(value) for value in self.answers[host]]


@pytest.mark.asyncio
async def test_download_pins_public_ip_and_sends_no_credentials():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"png")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = PublicMediaDownloader(
        ("media.example",),
        client=client,
        resolver=Resolver({"media.example": ["93.184.216.34"]}),
    )
    try:
        assert (
            await downloader.fetch(
                "https://media.example/files/a.png", deadline=time.monotonic() + 1
            )
            == b"png"
        )
        request = seen[0]
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "media.example"
        assert request.extensions["sni_hostname"] == "media.example"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_rejected_before_connection():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://redirect.example/private.png"})

    downloader = PublicMediaDownloader(
        ("media.example", "redirect.example"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=Resolver({"media.example": ["93.184.216.34"], "redirect.example": ["127.0.0.1"]}),
    )
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch(
                "https://media.example/files/a.png", deadline=time.monotonic() + 1
            )
        assert len(seen) == 1
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_unlisted_host_and_mixed_public_private_dns_are_rejected():
    downloader = PublicMediaDownloader(
        ("media.example",),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)),
        resolver=Resolver({"media.example": ["93.184.216.34", "169.254.169.254"]}),
    )
    try:
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://other.example/a", deadline=time.monotonic() + 1)
        with pytest.raises(UnsafeMediaSource):
            await downloader.fetch("https://media.example/a", deadline=time.monotonic() + 1)
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_transport_failure_retries_only_the_same_pinned_download():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, content=b"image")

    downloader = PublicMediaDownloader(
        ("media.example",),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=Resolver({"media.example": ["93.184.216.34"]}),
    )
    try:
        assert (
            await downloader.fetch("https://media.example/a", deadline=time.monotonic() + 1)
            == b"image"
        )
        assert calls == 2
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_owned_client_disables_cross_hostname_keepalive(monkeypatch):
    captured = {}
    real_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))

    def client_factory(**kwargs):
        captured.update(kwargs)
        return real_client

    monkeypatch.setattr(media_download_module.httpx, "AsyncClient", client_factory)
    downloader = PublicMediaDownloader(("one.example", "two.example"))
    try:
        assert captured["http2"] is False
        assert captured["limits"].max_keepalive_connections == 0
    finally:
        await downloader.close()
