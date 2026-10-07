"""Direct text generation and durable submission accounting."""

import asyncio
import time

import httpx
import pytest
import test_videos
from test_videos import OAuth

from grok_oauth.errors import Busy, InvalidRequest, OutcomeUnknown, PermissionDenied, ProtocolError
from grok_oauth.http import AuthorizedHttp
from grok_oauth.models import RequestPolicy
from grok_oauth.videos import GENERATION_MODEL, VideoRequest

# Reuse the real scoped storage fixture without recreating a second fixture family.
bundle = test_videos.bundle


@pytest.mark.parametrize(
    "prompt", ["  中文镜头，保持原文。\n", "  English prompt, keep exactly.\n"]
)
async def test_direct_text_payload_and_idempotent_task(bundle, prompt):
    bundle.http.responses.append({"request_id": "text-1"})
    request = VideoRequest(prompt, action="text")
    job = await bundle.service.submit(request, scope=bundle.scope, operation_key="text-message")
    again = await bundle.service.submit(request, scope=bundle.scope, operation_key="text-message")
    assert again.job_id == job.job_id
    assert job.submission_state == "submitted"
    assert len(bundle.http.calls) == 1
    method, path, body, policy = bundle.http.calls[0]
    assert (method, path) == ("POST", "/videos/generations")
    assert body == {
        "model": GENERATION_MODEL,
        "prompt": prompt,
        "duration": 6,
        "resolution": "480p",
    }
    assert policy.side_effecting and not policy.safe_pre_send_retries
    assert not bundle.downloader.calls
    with pytest.raises(ProtocolError):
        await bundle.service.submit(
            VideoRequest(prompt + "!", action="text"),
            scope=bundle.scope,
            operation_key="text-message",
        )
    assert len(bundle.http.calls) == 1


async def test_text_concurrent_duplicate_only_creates_one_upstream_job(bundle):
    bundle.http.gate = asyncio.Event()
    bundle.http.responses.append({"request_id": "text-1"})
    first = asyncio.create_task(
        bundle.service.submit(
            VideoRequest("prompt", action="text"), scope=bundle.scope, operation_key="same"
        )
    )
    await bundle.http.called.wait()
    second = await bundle.service.submit(
        VideoRequest("prompt", action="text"), scope=bundle.scope, operation_key="same"
    )
    assert second.status == "unknown" and second.submission_state == "unknown"
    bundle.http.gate.set()
    submitted = await first
    assert submitted.job_id == second.job_id and submitted.submission_state == "submitted"
    assert len(bundle.http.calls) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        VideoRequest("", "", action="text"),
        VideoRequest("prompt", "unexpected", action="text"),
        VideoRequest("prompt", "", action="text", duration=7),
        VideoRequest("prompt", "", action="text", resolution="720p"),
    ],
)
async def test_invalid_text_inputs_are_rejected_before_job_or_network(bundle, invalid):
    with pytest.raises(InvalidRequest):
        await bundle.service.submit(invalid, scope=bundle.scope, operation_key="bad")
    assert not bundle.http.calls
    assert not await bundle.store.list_jobs(scope=bundle.scope)


@pytest.mark.parametrize(
    "error,state,status",
    [(PermissionDenied(), "not_submitted", "failed"), (OutcomeUnknown(), "unknown", "unknown")],
)
async def test_submission_failure_reveals_operation_and_never_resubmits(
    bundle, error, state, status
):
    bundle.http.responses.append(error)
    request = VideoRequest("prompt", action="text")
    with pytest.raises(type(error)) as caught:
        await bundle.service.submit(request, scope=bundle.scope, operation_key="failed")
    summary = caught.value.summary()
    assert summary["operation_id"] and summary["submission_state"] == state
    again = await bundle.service.submit(request, scope=bundle.scope, operation_key="failed")
    assert again.job_id == summary["operation_id"]
    assert again.status == status and again.submission_state == state
    assert len(bundle.http.calls) == 1


async def test_missing_reference_failure_is_explicitly_not_submitted(bundle):
    with pytest.raises(Exception) as caught:
        await bundle.service.submit(
            VideoRequest("prompt", "f" * 32), scope=bundle.scope, operation_key="missing"
        )
    assert caught.value.summary()["submission_state"] == "not_submitted"
    assert caught.value.operation_id
    assert not bundle.http.calls


@pytest.mark.parametrize(
    "side_effecting,expected", [(True, OutcomeUnknown), (False, ProtocolError)]
)
async def test_nonobject_success_response_is_unknown_only_for_paid_submission(
    side_effecting, expected
):
    async def handler(request):
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    http = AuthorizedHttp(client, OAuth())
    try:
        with pytest.raises(expected):
            await http.request_json(
                "POST",
                "/videos/generations",
                json={"prompt": "test"},
                policy=RequestPolicy(
                    capability="videos",
                    deadline=time.monotonic() + 5,
                    side_effecting=side_effecting,
                ),
            )
    finally:
        await client.aclose()


@pytest.mark.parametrize("response", [{}, {"request_id": "bad id"}, []])
async def test_accepted_response_without_recoverable_handle_is_unknown(bundle, response):
    bundle.http.responses.append(response)
    with pytest.raises(OutcomeUnknown) as caught:
        await bundle.service.submit(
            VideoRequest("prompt", action="text"), scope=bundle.scope, operation_key="lost-handle"
        )
    assert caught.value.summary()["submission_state"] == "unknown"
    duplicate = await bundle.service.submit(
        VideoRequest("prompt", action="text"), scope=bundle.scope, operation_key="lost-handle"
    )
    assert duplicate.status == "unknown" and duplicate.job_id == caught.value.operation_id
    assert len(bundle.http.calls) == 1


@pytest.mark.parametrize("status,expected", [(400, "not_submitted"), (500, "unknown")])
async def test_real_http_submission_failure_keeps_precise_accounting_state(
    bundle, status, expected
):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": "private upstream text"})
        )
    )
    bundle.service.http = AuthorizedHttp(client, bundle.oauth)
    try:
        with pytest.raises((ProtocolError, OutcomeUnknown)) as caught:
            await bundle.service.submit(
                VideoRequest("prompt", action="text"),
                scope=bundle.scope,
                operation_key="http-state",
            )
        assert caught.value.summary()["submission_state"] == expected
        job = await bundle.service.check_job(caught.value.operation_id, scope=bundle.scope)
        assert job.submission_state == expected
        assert "private upstream text" not in str(caught.value)
    finally:
        await client.aclose()


async def test_text_cancellation_after_post_keeps_unknown_task_without_retry(bundle):
    bundle.http.gate = asyncio.Event()
    task = asyncio.create_task(
        bundle.service.submit(
            VideoRequest("prompt", action="text"), scope=bundle.scope, operation_key="cancel-text"
        )
    )
    await bundle.http.called.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    duplicate = await bundle.service.submit(
        VideoRequest("prompt", action="text"), scope=bundle.scope, operation_key="cancel-text"
    )
    assert duplicate.status == "unknown" and duplicate.submission_state == "unknown"
    assert len(bundle.http.calls) == 1


@pytest.mark.parametrize(
    "received_handle,state,error_type",
    [(False, "unknown", OutcomeUnknown), (True, "submitted", Busy)],
)
async def test_storage_cleanup_failure_cannot_downgrade_submit_state(
    bundle, monkeypatch, received_handle, state, error_type
):
    real_get = bundle.store.get_job
    real_update = bundle.store.update_job

    async def fail_read(*args, **kwargs):
        raise ProtocolError("metadata read failed")

    async def fail_ack_write(*args, **kwargs):
        if kwargs.get("request_id"):
            raise Busy("metadata lock busy")
        return await real_update(*args, **kwargs)

    monkeypatch.setattr(bundle.store, "get_job", fail_read)
    monkeypatch.setattr(bundle.store, "update_job", fail_ack_write)
    bundle.http.responses.append(
        {"request_id": "accepted-remote"} if received_handle else OutcomeUnknown()
    )
    request = VideoRequest("prompt", action="text")
    with pytest.raises(error_type) as caught:
        await bundle.service.submit(request, scope=bundle.scope, operation_key="cleanup-failed")
    assert caught.value.operation_id and caught.value.summary()["submission_state"] == state
    monkeypatch.setattr(bundle.store, "get_job", real_get)
    monkeypatch.setattr(bundle.store, "update_job", real_update)
    duplicate = await bundle.service.submit(
        request, scope=bundle.scope, operation_key="cleanup-failed"
    )
    assert duplicate.job_id == caught.value.operation_id and duplicate.status == "unknown"
    assert duplicate.submission_state == "unknown" and len(bundle.http.calls) == 1
