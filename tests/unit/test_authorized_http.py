import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from grok_oauth.errors import (
    AuthorizationChanged,
    ClientNotEligible,
    OutcomeUnknown,
    ProtocolError,
    RateLimited,
    ReauthorizationRequired,
    UnsafeTarget,
)
from grok_oauth.http import AuthorizedHttp
from grok_oauth.models import RequestPolicy, TokenSnapshot


class DiagnosticLogger:
    def __init__(self, fail=False):
        self.records = []
        self.fail = fail

    def info(self, template, value):
        if self.fail:
            raise RuntimeError("logger unavailable")
        assert template == "grok_oauth_transport_diag %s"
        self.records.append(json.loads(value))


@pytest.mark.asyncio
async def test_transport_diagnostics_disabled_by_default():
    logger = DiagnosticLogger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True}))
    ) as client:
        assert await AuthorizedHttp(client, OAuthStub(), diagnostic_logger=logger).request_json(
            "POST", "/responses", json={"secret": "SENTINEL"}, policy=policy()
        ) == {"ok": True}
        AuthorizedHttp(client, OAuthStub(), diagnostic_logger=logger).log_completion(
            mode="chat",
            provider_id="grok_oauth/grok-4.7",
            configured_model="grok-4.7",
            result=SimpleNamespace(model="grok-4.7", id="resp-id"),
        )

    def failing_json_handler(request):
        raise httpx.ReadError("SENTINEL", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing_json_handler)) as client:
        with pytest.raises(ProtocolError):
            await AuthorizedHttp(client, OAuthStub(), diagnostic_logger=logger).request_json(
                "GET", "/models", policy=policy()
            )

    class FailingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            raise httpx.ReadError("SENTINEL")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"ok"))
    ) as client:
        async with AuthorizedHttp(client, OAuthStub(), diagnostic_logger=logger).stream_sse(
            "/responses", json={}, policy=policy()
        ) as chunks:
            assert [chunk async for chunk in chunks] == [b"ok"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=FailingBody()))
    ) as client:
        with pytest.raises(ProtocolError):
            async with AuthorizedHttp(client, OAuthStub(), diagnostic_logger=logger).stream_sse(
                "/responses", json={}, policy=policy()
            ) as chunks:
                assert await anext(chunks) == b"first"
                await anext(chunks)
    assert logger.records == []


@pytest.mark.asyncio
async def test_transport_diagnostics_success_retry_and_safe_fields():
    logger = DiagnosticLogger()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("SENTINEL", request=request)
        return httpx.Response(
            200, json={"secret": "SENTINEL"}, headers={"x-request-id": "safe-id-1"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await AuthorizedHttp(
            client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
        ).request_json("POST", "/responses", json={"secret": "SENTINEL"}, policy=policy())
    assert len(logger.records) == 1
    record = logger.records[0]
    assert record["outcome"] == "success"
    assert record["request_id"] == "safe-id-1"
    assert record["status"] == 200
    assert record["connect_retries"] == 1
    assert record["elapsed_ms"] >= 0
    assert "SENTINEL" not in json.dumps(record)


@pytest.mark.asyncio
async def test_transport_diagnostics_classify_failure_and_fail_open():
    logger = DiagnosticLogger()

    def handler(request):
        raise httpx.ReadTimeout("SENTINEL", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            await AuthorizedHttp(
                client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
            ).request_json("GET", "/models", policy=policy())
        with pytest.raises(ProtocolError):
            await AuthorizedHttp(
                client,
                OAuthStub(),
                diagnostic_logging=True,
                diagnostic_logger=DiagnosticLogger(fail=True),
            ).request_json("GET", "/models", policy=policy())
    assert logger.records[0]["cause"] == "ReadTimeout"
    assert logger.records[0]["phase"] == "send_or_headers"
    assert logger.records[0]["headers_received"] is False
    assert logger.records[0]["status"] is None
    assert "SENTINEL" not in json.dumps(logger.records)


@pytest.mark.asyncio
async def test_stream_diagnostics_classify_body_failure_and_cancellation():
    class FailingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: first\\n\\n"
            raise httpx.ReadError("SENTINEL")

    logger = DiagnosticLogger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=FailingBody()))
    ) as client:
        with pytest.raises(ProtocolError):
            async with AuthorizedHttp(
                client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
            ).stream_sse("/responses", json={"secret": "SENTINEL"}, policy=policy()) as chunks:
                assert await anext(chunks) == b"data: first\\n\\n"
                await anext(chunks)
    assert logger.records[0]["outcome"] == "failure"
    assert logger.records[0]["cause"] == "ReadError"
    assert logger.records[0]["phase"] == "stream_body"
    assert "SENTINEL" not in json.dumps(logger.records)

    class CancelledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise asyncio.CancelledError()
            yield b"unreachable"

    logger = DiagnosticLogger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=CancelledBody()))
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            async with AuthorizedHttp(
                client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
            ).stream_sse("/responses", json={}, policy=policy()) as chunks:
                await anext(chunks)
    assert logger.records[0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_provider_completion_diagnostic_validates_identifiers():
    logger = DiagnosticLogger()
    async with httpx.AsyncClient() as client:
        transport = AuthorizedHttp(
            client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
        )
        transport.log_completion(
            mode="chat",
            provider_id="grok_oauth/grok-4.7",
            configured_model="grok-4.7",
            result=SimpleNamespace(model="grok-4.7", id="resp_Safe-1"),
        )
        transport.log_completion(
            mode="stream",
            provider_id="secret\nSENTINEL",
            configured_model="grok-4.7",
            result=SimpleNamespace(model="grok-4.7", id="resp\nSENTINEL"),
        )
        transport.log_completion(
            mode="chat",
            provider_id="https://secret/path",
            configured_model="/var/secret/model",
            result=SimpleNamespace(model="C:/secret/model", id="file:///secret/response"),
        )
    assert logger.records[0]["provider_id"] == "grok_oauth/grok-4.7"
    assert logger.records[0]["response_id"] == "resp_Safe-1"
    assert logger.records[1]["provider_id"] == logger.records[1]["response_id"] == ""
    assert all(
        logger.records[2][field] == ""
        for field in ("provider_id", "configured_model", "actual_model", "response_id")
    )
    assert "SENTINEL" not in json.dumps(logger.records)


@pytest.mark.asyncio
async def test_json_diagnostics_keep_headers_and_partial_body_on_read_error():
    class PartialBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"par"
            raise httpx.ReadError("SENTINEL")

    logger = DiagnosticLogger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"x-request-id": "upstream-42"}, stream=PartialBody()
            )
        )
    ) as client:
        with pytest.raises(ProtocolError):
            await AuthorizedHttp(
                client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
            ).request_json("GET", "/models", policy=policy())
    record = logger.records[0]
    assert record["phase"] == "read_body"
    assert record["cause"] == "ReadError"
    assert record["headers_received"] is True
    assert record["status"] == 200
    assert record["request_id"] == "upstream-42"
    assert record["body_bytes"] == 3
    assert record["body_exhausted"] is False
    assert "SENTINEL" not in json.dumps(record)


@pytest.mark.asyncio
async def test_json_diagnostics_clear_previous_401_before_failed_retry():
    logger = DiagnosticLogger()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401, headers={"x-request-id": "old-request"})
        raise httpx.ConnectError("SENTINEL", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            await AuthorizedHttp(
                client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
            ).request_json("GET", "/models", policy=policy(safe_pre_send_retries=0))
    record = logger.records[0]
    assert record["auth_retries"] == 1
    assert record["connect_retries"] == 0
    assert record["cause"] == "ConnectError"
    assert record["status"] is None
    assert record["request_id"] == ""
    assert record["headers_received"] is False


@pytest.mark.asyncio
async def test_stream_diagnostics_clear_previous_401_before_failed_retry():
    logger = DiagnosticLogger()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401, headers={"x-request-id": "old-stream-request"})
        raise httpx.ConnectError("SENTINEL", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            async with AuthorizedHttp(
                client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
            ).stream_sse("/responses", json={}, policy=policy(safe_pre_send_retries=0)):
                pytest.fail("The stream cannot start after connection failure")
    record = logger.records[0]
    assert record["auth_retries"] == 1
    assert record["cause"] == "ConnectError"
    assert record["status"] is None
    assert record["request_id"] == ""
    assert record["headers_received"] is False


@pytest.mark.asyncio
async def test_stream_scope_return_is_not_reported_as_completed_response():
    class TwoChunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            yield b"second"

    logger = DiagnosticLogger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=TwoChunks()))
    ) as client:
        transport = AuthorizedHttp(
            client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
        )
        async with transport.stream_sse("/responses", json={}, policy=policy()) as chunks:
            assert await anext(chunks) == b"first"
        async with transport.stream_sse("/responses", json={}, policy=policy()) as chunks:
            assert [chunk async for chunk in chunks] == [b"first", b"second"]
    assert [record["outcome"] for record in logger.records] == [
        "scope_returned",
        "stream_exhausted",
    ]
    assert [record["body_exhausted"] for record in logger.records] == [False, True]


class OAuthStub:
    def __init__(self):
        self.calls = []
        self.current = TokenSnapshot("default", "oauth-one", "refresh", None, "s", "c", 1, 0)

    async def get_token(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("force_refresh"):
            self.current = TokenSnapshot("default", "oauth-two", "refresh", None, "s", "c", 2, 0)
        return self.current


def policy(**changes):
    values = {
        "capability": "chat",
        "deadline": 10**9,
        "safe_pre_send_retries": 1,
        "allow_one_auth_retry": True,
        "side_effecting": False,
    }
    values.update(changes)
    return RequestPolicy(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["https://evil.example/x", "//evil.example/x", "/%2f%2fevil", "/v1/../token", "http:/evil"],
)
async def test_unsafe_paths_rejected_before_network(path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeTarget):
            await AuthorizedHttp(client, OAuthStub()).request_json(
                "POST", path, json={}, policy=policy()
            )
    assert calls == 0


@pytest.mark.asyncio
async def test_unsafe_bearer_value_is_rejected_before_network():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    oauth = OAuthStub()
    oauth.current = TokenSnapshot("default", "bad\r\nheader", "refresh", None, "s", "c", 1, 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ReauthorizationRequired):
            await AuthorizedHttp(client, oauth).request_json(
                "GET", "/models", json=None, policy=policy()
            )
    assert calls == 0


@pytest.mark.asyncio
async def test_nonfinite_deadline_is_rejected_before_auth_or_network():
    oauth = OAuthStub()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    ) as client:
        with pytest.raises(ProtocolError):
            await AuthorizedHttp(client, oauth).request_json(
                "GET", "/models", json=None, policy=policy(deadline=float("nan"))
            )
    assert oauth.calls == []


@pytest.mark.asyncio
async def test_oauth_only_bearer_and_one_401_refresh(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-sentinel-must-not-be-used")
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["authorization"]))
        return httpx.Response(401 if len(seen) < 2 else 200, json={"ok": True})

    oauth = OAuthStub()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        assert await AuthorizedHttp(client, oauth).request_json(
            "POST", "/responses", json={}, policy=policy()
        ) == {"ok": True}
    assert seen == [("api.x.ai", "Bearer oauth-one"), ("api.x.ai", "Bearer oauth-two")]
    assert oauth.calls == [{}, {"force_refresh": True, "rejected_version": 1}]


@pytest.mark.asyncio
async def test_second_401_terminal_and_redirect_never_followed():
    calls = []

    def reject(request):
        calls.append(request.url.host)
        return httpx.Response(
            401, json={"access_token": "leak"}, headers={"x-request-id": "safe-1"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(ReauthorizationRequired) as caught:
            await AuthorizedHttp(client, OAuthStub()).request_json(
                "POST", "/responses", json={}, policy=policy()
            )
    assert (
        calls == ["api.x.ai", "api.x.ai"]
        and caught.value.request_id == "safe-1"
        and "leak" not in str(caught.value)
    )
    hosts = []

    def redirect(request):
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect), follow_redirects=True
    ) as client:
        with pytest.raises(UnsafeTarget):
            await AuthorizedHttp(client, OAuthStub()).request_json(
                "POST", "/responses", json={}, policy=policy()
            )
    assert hosts == ["api.x.ai"]


@pytest.mark.asyncio
async def test_presend_retry_but_side_effect_read_failure_never_retries():
    calls = 0

    def connect_once(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("pre-send", request=request)
        return httpx.Response(200, json={"ok": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect_once)) as client:
        assert await AuthorizedHttp(client, OAuthStub()).request_json(
            "GET", "/models", json=None, policy=policy()
        ) == {"ok": 1}
    calls = 0

    def read_fail(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("after-send", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(read_fail)) as client:
        with pytest.raises(OutcomeUnknown):
            await AuthorizedHttp(client, OAuthStub()).request_json(
                "POST",
                "/images/generations",
                json={},
                policy=policy(side_effecting=True, safe_pre_send_retries=5),
            )
    assert calls == 1


@pytest.mark.asyncio
async def test_json_bound_and_stream_read_failure_are_unknown():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b'{"data":"' + b"x" * 100 + b'"}')
        )
    ) as client:
        with pytest.raises(OutcomeUnknown):
            await AuthorizedHttp(client, OAuthStub(), max_json_bytes=32).request_json(
                "POST", "/images/generations", json={}, policy=policy(side_effecting=True)
            )

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: first\n\n"
            raise httpx.ReadError("broken")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Broken()))
    ) as client:
        async with AuthorizedHttp(client, OAuthStub()).stream_sse(
            "/responses", json={}, policy=policy(side_effecting=True)
        ) as chunks:
            assert await anext(chunks) == b"data: first\n\n"
            with pytest.raises(OutcomeUnknown):
                await anext(chunks)


@pytest.mark.asyncio
async def test_safe_403_classification_and_rate_limit_budget():
    responses = iter(
        [
            httpx.Response(403, json={"error": "client_not_eligible", "detail": "secret"}),
            httpx.Response(429, json={"error": "rate"}, headers={"retry-after": "60"}),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: next(responses))
    ) as client:
        transport = AuthorizedHttp(client, OAuthStub())
        with pytest.raises(ClientNotEligible) as denied:
            await transport.request_json("GET", "/models", json=None, policy=policy())
        assert "secret" not in str(denied.value)
        with pytest.raises(RateLimited) as limited:
            await transport.request_json("GET", "/models", json=None, policy=policy(deadline=10**9))
        assert 0 <= limited.value.retry_after_seconds <= 60


class SlowBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"value":"'
        await asyncio.sleep(0.06)
        yield b'ok"}'


@pytest.mark.asyncio
async def test_request_json_enforces_absolute_deadline_during_slow_body():
    def handler(request):
        return httpx.Response(200, stream=SlowBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OutcomeUnknown):
            await AuthorizedHttp(client, OAuthStub()).request_json(
                "POST",
                "/images/generations",
                json={},
                policy=policy(deadline=time.monotonic() + 0.01, side_effecting=True),
            )


@pytest.mark.asyncio
async def test_stream_enforces_absolute_deadline_for_each_chunk():
    def handler(request):
        return httpx.Response(200, stream=SlowBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with AuthorizedHttp(client, OAuthStub()).stream_sse(
            "/responses",
            json={},
            policy=policy(deadline=time.monotonic() + 0.01, side_effecting=True),
        ) as chunks:
            assert await anext(chunks) == b'{"value":"'
            with pytest.raises(OutcomeUnknown):
                await anext(chunks)


@pytest.mark.asyncio
async def test_stream_rejects_cached_chunk_after_consumer_exceeds_deadline():
    class ImmediateBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            yield b"second"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=ImmediateBody()))
    ) as client:
        async with AuthorizedHttp(client, OAuthStub()).stream_sse(
            "/responses",
            json={},
            policy=policy(deadline=time.monotonic() + 0.01, side_effecting=True),
        ) as chunks:
            assert await anext(chunks) == b"first"
            await asyncio.sleep(0.032)
            with pytest.raises(OutcomeUnknown):
                await anext(chunks)


@pytest.mark.asyncio
async def test_authentication_wait_is_part_of_absolute_deadline():
    class SlowOAuth(OAuthStub):
        def __init__(self):
            super().__init__()
            self.cancelled = False

        async def get_token(self, **kwargs):
            try:
                await asyncio.sleep(0.06)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return await super().get_token(**kwargs)

    network_calls = 0

    def handler(request):
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    oauth = SlowOAuth()
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            await AuthorizedHttp(client, oauth).request_json(
                "GET", "/models", json=None, policy=policy(deadline=started + 0.01)
            )
    assert time.monotonic() - started < 0.05
    assert oauth.cancelled and network_calls == 0


class EpochSwitchOAuth(OAuthStub):
    async def get_token(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("force_refresh"):
            self.current = TokenSnapshot("default", "account-b", "refresh-b", None, "s", "c", 1, 1)
        return self.current


@pytest.mark.asyncio
async def test_json_401_never_replays_under_new_authorization_epoch():
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthorizationChanged):
            await AuthorizedHttp(client, EpochSwitchOAuth()).request_json(
                "POST", "/images/generations", json={}, policy=policy(side_effecting=True)
            )
    assert seen == ["Bearer oauth-one"]


@pytest.mark.asyncio
async def test_stream_401_never_replays_under_new_authorization_epoch():
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, content=b"data: ok\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthorizationChanged):
            async with AuthorizedHttp(client, EpochSwitchOAuth()).stream_sse(
                "/responses", json={}, policy=policy(side_effecting=True)
            ):
                pass
    assert seen == ["Bearer oauth-one"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_payment_required_is_typed_without_refresh_or_replay(streaming):
    from grok_oauth.errors import PermissionDenied

    calls = []
    oauth = OAuthStub()

    def handler(request):
        calls.append(request)
        return httpx.Response(
            402, json={"error": "billing SECRET_SENTINEL"}, headers={"x-request-id": "payment-test"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = AuthorizedHttp(client, oauth)
        with pytest.raises(PermissionDenied) as caught:
            if streaming:
                async with transport.stream_sse("/responses", json={}, policy=policy()):
                    pytest.fail("A denied stream must not start")
            else:
                await transport.request_json("POST", "/responses", json={}, policy=policy())
    assert caught.value.code == "PaymentRequired"
    assert caught.value.request_id == "payment-test"
    assert "SECRET_SENTINEL" not in str(caught.value)
    assert len(calls) == 1 and oauth.calls == [{}]


@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout]
)
async def test_transport_timeout_diagnostics_preserve_safe_class_and_retry_policy(error_type):
    attempts = 0
    logger = DiagnosticLogger()

    async def handler(request):
        nonlocal attempts
        attempts += 1
        raise error_type("private-upstream-details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError):
            await AuthorizedHttp(
                client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
            ).request_json("POST", "/responses", json={}, policy=policy())
    assert attempts == (2 if error_type is httpx.ConnectTimeout else 1)
    assert logger.records[0]["outcome"] == "timeout"
    assert logger.records[0]["termination_reason"] == "transport_timeout"
    assert logger.records[0]["cause"] == error_type.__name__
    assert "private-upstream-details" not in json.dumps(logger.records)


@pytest.mark.parametrize("side_effecting", [False, True])
async def test_stream_consumer_crossing_deadline_is_diagnosed_as_timeout(side_effecting):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            yield b"second"

    logger = DiagnosticLogger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body()))
    ) as client:
        transport = AuthorizedHttp(
            client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
        )
        request_policy = policy(deadline=time.monotonic() + 0.05, side_effecting=side_effecting)
        with pytest.raises(OutcomeUnknown if side_effecting else ProtocolError):
            async with transport.stream_sse("/responses", json={}, policy=request_policy) as chunks:
                assert await anext(chunks) == b"first"
                await asyncio.sleep(0.06)
                await anext(chunks)
    assert logger.records[0]["outcome"] == "timeout"
    assert logger.records[0]["termination_reason"] == "request_deadline"
    assert logger.records[0]["headers_received"] is True
    assert logger.records[0]["body_bytes"] == 5


@pytest.mark.parametrize("streaming", [False, True])
async def test_external_cancellation_keeps_origin_when_cleanup_crosses_deadline(
    streaming, monkeypatch
):
    import grok_oauth.http as transport_module

    entered = asyncio.Event()
    closed = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            # Advance only the diagnostic clock during cleanup, without changing
            # asyncio timer scheduling or the existing cancellation behavior.
            monkeypatch.setattr(
                transport_module,
                "time",
                SimpleNamespace(monotonic=lambda: request_policy.deadline + 1),
            )
            closed.set()

    logger = DiagnosticLogger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body()))
    ) as client:
        transport = AuthorizedHttp(
            client, OAuthStub(), diagnostic_logging=True, diagnostic_logger=logger
        )
        request_policy = policy(deadline=time.monotonic() + 0.05)

        async def read():
            if streaming:
                async with transport.stream_sse(
                    "/responses", json={}, policy=request_policy
                ) as chunks:
                    await anext(chunks)
            else:
                await transport.request_json("POST", "/responses", json={}, policy=request_policy)

        task = asyncio.create_task(read())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    assert logger.records[0]["outcome"] == "cancelled"
    assert logger.records[0]["termination_reason"] == "external_cancel"


@pytest.mark.asyncio
async def test_diagnostics_without_injected_logger_do_not_create_fallback_logs(caplog):
    caplog.set_level("INFO")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True}))
    ) as client:
        transport = AuthorizedHttp(client, OAuthStub(), diagnostic_logging=True)
        assert await transport.request_json("GET", "/models", policy=policy()) == {"ok": True}
        transport.log_completion(
            mode="chat",
            provider_id="grok-test",
            configured_model="grok-4.7",
            result=SimpleNamespace(model="grok-4.7", id="response-test"),
        )
    assert not [r for r in caplog.records if "grok_oauth_transport_diag" in r.getMessage()]
