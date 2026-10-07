import asyncio
import json
import time

import httpx
import pytest
from astrbot_plugin_grok_oauth.astrbot_adapter.runtime import GrokRuntime
from astrbot_plugin_grok_oauth.grok_oauth.errors import (
    InvalidRequest,
    PermissionDenied,
    ServiceClosed,
)
from astrbot_plugin_grok_oauth.grok_oauth.models import DeviceFlow, TokenSnapshot


async def test_runtime_config_controls_transport_diagnostics(tmp_path):
    class Logger:
        def __init__(self):
            self.records = []

        def info(self, template, value):
            self.records.append(json.loads(value))

    class OAuth:
        async def get_token(self, **kwargs):
            return TokenSnapshot("default", "test-access", "refresh", None, "s", "c", 1, 0)

    from astrbot_plugin_grok_oauth.grok_oauth.models import RequestPolicy

    logger = Logger()
    for enabled in (False, True):
        rt = GrokRuntime(
            {"transport_diagnostics": enabled},
            tmp_path / str(enabled),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
            diagnostic_logger=logger,
        )
        rt.http._oauth = OAuth()
        try:
            result = await rt.http.request_json(
                "GET", "/models", policy=RequestPolicy(deadline=time.monotonic() + 10)
            )
            assert result == {"ok": True}
            assert len(logger.records) == int(enabled)
        finally:
            await rt.client.aclose()


@pytest.fixture
async def runtime(tmp_path):
    rt = GrokRuntime(
        {},
        tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []})),
    )
    await rt.open()
    yield rt
    await rt.close()


class FakeWire:
    def __init__(self):
        self.started = 0
        self.gate = asyncio.Event()

    async def start_device_flow(self, *, owner_id, epoch):
        self.started += 1
        return DeviceFlow(
            "flow-test",
            owner_id,
            "TEST-CODE",
            "https://auth.x.ai/device",
            "secret-device",
            60,
            1,
            epoch,
            time.monotonic() + 60,
            time.time() + 60,
        )

    async def poll_device_flow(self, flow):
        await self.gate.wait()
        return TokenSnapshot(
            "default",
            "test-access",
            "test-refresh",
            time.time() + 3600,
            "api:access",
            "b1a00492-073a-47ea-816f-4c329264a828",
        )


def consent(runtime):
    return {"confirmed_client_id": runtime.client_id, "client_profile": runtime.client_profile}


async def test_login_requires_explicit_current_client_consent(runtime):
    runtime.wire = FakeWire()
    with pytest.raises(InvalidRequest):
        await runtime.start_flow("admin")
    assert runtime.wire.started == 0
    result = await runtime.start_flow("admin", **consent(runtime))
    assert result["status"] == "pending" and result["user_code"] == "TEST-CODE"
    assert "secret-device" not in str(result)


async def test_concurrent_same_owner_login_is_single_flow(runtime):
    runtime.wire = FakeWire()
    results = await asyncio.gather(
        *(runtime.start_flow("admin", **consent(runtime)) for _ in range(20))
    )
    assert runtime.wire.started == 1 and {r["flow_id"] for r in results} == {"flow-test"}
    with pytest.raises(PermissionDenied):
        runtime.status("other", "flow-test")
    with pytest.raises(PermissionDenied):
        await runtime.cancel_flow("other", "flow-test")


async def test_cancel_then_late_authorization_does_not_bind(runtime):
    wire = runtime.wire = FakeWire()
    await runtime.start_flow("admin", **consent(runtime))
    await runtime.cancel_flow("admin", "flow-test")
    wire.gate.set()
    await asyncio.sleep(0)
    assert runtime.oauth.snapshot() is None
    assert runtime.status("admin")["status"] == "cancelled"


async def test_success_persists_and_status_contains_no_credentials(runtime):
    wire = runtime.wire = FakeWire()
    await runtime.start_flow("admin", **consent(runtime))
    wire.gate.set()
    await runtime.wait_for_flow()
    status = runtime.status("admin", "flow-test")
    assert status["status"] == "authorized"
    assert "test-access" not in str(status) and "test-refresh" not in str(status)
    assert runtime.oauth.snapshot().access_token == "test-access"
    await runtime.disconnect("admin")
    assert runtime.oauth.snapshot() is None


async def test_close_cancels_flow_and_releases_store(runtime):
    runtime.wire = FakeWire()
    await runtime.start_flow("admin", **consent(runtime))
    await runtime.close()
    assert runtime.closed and not runtime.tasks
    with pytest.raises(ServiceClosed):
        await runtime.start_flow("admin", **consent(runtime))


async def test_cancel_close_does_not_abandon_cleanup(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowProvider:
        async def terminate(self):
            entered.set()
            await release.wait()

    provider = SlowProvider()
    runtime.providers.add(provider)
    closing = asyncio.create_task(runtime.close())
    await entered.wait()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await runtime.close()
    assert runtime.client.is_closed and runtime.oauth.status == "closed"


async def test_open_and_close_are_serialized(runtime, tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    rt = GrokRuntime(
        {},
        tmp_path / "race",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    construct = rt._construct_assets

    async def slow_construct():
        entered.set()
        await release.wait()
        await construct()

    monkeypatch.setattr(rt, "_construct_assets", slow_construct)
    opening = asyncio.create_task(rt.open())
    await entered.wait()
    closing = asyncio.create_task(rt.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await opening
    await closing
    assert rt.closed and not rt._opened and rt.assets._closed and rt.client.is_closed


async def test_cancel_open_waits_for_asset_ownership_then_cleans(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    rt = GrokRuntime(
        {},
        tmp_path / "cancel",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    construct = rt._construct_assets

    async def slow_construct():
        entered.set()
        await release.wait()
        await construct()

    monkeypatch.setattr(rt, "_construct_assets", slow_construct)
    opening = asyncio.create_task(rt.open())
    await entered.wait()
    opening.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert rt.closed and rt.assets._closed and rt.client.is_closed


@pytest.mark.parametrize("enabled", [False, True])
async def test_default_runtime_diagnostics_use_public_astrbot_logger(
    tmp_path, monkeypatch, enabled
):
    from types import SimpleNamespace

    from astrbot_plugin_grok_oauth.astrbot_adapter import diagnostics as module
    from astrbot_plugin_grok_oauth.grok_oauth.models import RequestPolicy

    records = []
    monkeypatch.setattr(module, "logger", SimpleNamespace(info=lambda *args: records.append(args)))

    class OAuth:
        async def get_token(self, **kwargs):
            return TokenSnapshot("default", "synthetic-access", "refresh", None, "s", "c", 1, 0)

    rt = GrokRuntime(
        {"transport_diagnostics": enabled},
        tmp_path,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True})),
    )
    rt.http._oauth = OAuth()
    try:
        assert await rt.http.request_json(
            "GET", "/models", policy=RequestPolicy(deadline=time.monotonic() + 10)
        ) == {"ok": True}
        assert len(records) == int(enabled)
        if enabled:
            assert records[0][0] == "grok_oauth_transport_diag %s"
            assert json.loads(records[0][1])["outcome"] == "success"
            assert "synthetic-access" not in records[0][1]
    finally:
        await rt.client.aclose()


async def test_runtime_file_diagnostics_independent_of_native_logging_and_drained_on_close(
    tmp_path,
):
    from astrbot_plugin_grok_oauth.grok_oauth.models import RequestPolicy

    class NativeLogger:
        def info(self, *args):
            raise AssertionError("native logging is disabled")

    class OAuth:
        async def get_token(self, **kwargs):
            return TokenSnapshot("default", "file-test-access", "refresh", None, "s", "c", 1, 0)

        async def close(self):
            pass

    rt = GrokRuntime(
        {"transport_diagnostics": False, "transport_debug_file": True},
        tmp_path,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True})),
        diagnostic_logger=NativeLogger(),
    )
    rt.http._oauth = OAuth()
    try:
        assert await rt.http.request_json(
            "GET", "/models", policy=RequestPolicy(deadline=time.monotonic() + 10)
        ) == {"ok": True}
    finally:
        await rt.close()
    records = [
        json.loads(line) for line in (tmp_path / "debug/transport.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["outcome"] == "success"
    assert "file-test-access" not in json.dumps(records)
