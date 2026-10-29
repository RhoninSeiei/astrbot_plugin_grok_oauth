"""Capability negotiation and direct generation through the public Provider API."""

import pytest
import test_video_api
from astrbot_plugin_grok_oauth.grok_oauth.errors import (
    InvalidRequest,
    PermissionDenied,
    UnsafeMediaSource,
    VideoUnsupported,
)

# Keep the same actual Provider/service/storage fixture as the existing API tests.
env = test_video_api.env


async def test_capabilities_are_network_free_and_truthful(env):
    env.oauth.status = "authorized"
    capabilities = await env.provider.get_video_capabilities(event=env.event)
    assert capabilities["schema_version"] == 1
    assert capabilities["available"] is True
    assert "text_to_video" in capabilities["modes"]
    assert "image_to_video" in capabilities["modes"]
    assert "video_edit" in capabilities["modes"]
    assert "video_edit_with_images" not in capabilities["modes"]
    assert (
        capabilities["unsupported_modes"]["video_edit_with_images"]
        == "upstream_reference_edit_combination_unsupported"
    )
    assert capabilities["durations"] == [6, 10]
    assert capabilities["resolution"] == "480p"
    assert capabilities["max_reference_images"] == 1
    assert capabilities["max_video_bytes"] == 20971520
    assert capabilities["max_edit_duration"] == 8.7
    assert capabilities["account_entitlement"] == "unknown"
    assert not env.http.calls


@pytest.mark.parametrize("changed", ["disabled", "unbound", "reauth_required"])
async def test_capabilities_do_not_offer_paid_modes_when_unavailable(env, changed):
    env.oauth.status = changed if changed != "disabled" else "authorized"
    if changed == "disabled":
        env.runtime.config["videos_enabled"] = False
    capabilities = await env.provider.get_video_capabilities(event=env.event)
    assert capabilities["available"] is False and capabilities["modes"] == []
    assert not env.http.calls


async def test_public_text_generation_preserves_prompt_and_caller_owns_delivery(env):
    prompt = "  人物走到窗边。\nKeep original text.  "
    job = await env.provider.submit_text_video(prompt, event=env.event, operation_key="caller:text")
    assert job["status"] == "pending" and job["submission_state"] == "submitted"
    assert job["delivery_owner"] == "caller"
    assert env.http.calls[0][2]["prompt"] == prompt
    assert "image" not in env.http.calls[0][2]
    again = await env.provider.submit_text_video(
        prompt, event=env.event, operation_key="caller:text"
    )
    assert again == job and len(env.http.calls) == 1
    done = await env.provider.wait_video_job(job["job_id"], event=env.event)
    assert done["status"] == "done" and done["submission_state"] == "submitted"
    assert await env.provider.read_video_bytes(done["job_id"], event=env.event)
    assert not env.provider._tasks


@pytest.mark.parametrize("key", ["", " ", None, False, 1, "x" * 257])
async def test_text_requires_explicit_idempotency_key_before_upstream(env, key):
    with pytest.raises(InvalidRequest):
        await env.provider.submit_text_video("prompt", event=env.event, operation_key=key)
    assert not env.http.calls


async def test_joint_edit_is_explicitly_unsupported_without_dropping_inputs(env):
    images = ["ordered-reference-image"]
    with pytest.raises(VideoUnsupported) as caught:
        await env.provider.edit_video_with_images(
            " exact prompt ", "source-video", images, event=env.event, operation_key="joint"
        )
    assert caught.value.summary()["submission_state"] == "not_submitted"
    assert "upstream_reference_edit_combination_unsupported" in str(caught.value)
    assert images == ["ordered-reference-image"] and not env.http.calls


async def test_text_respects_enabled_configuration(env):
    env.runtime.config["videos_enabled"] = False
    with pytest.raises(PermissionDenied):
        await env.provider.submit_text_video("prompt", event=env.event, operation_key="disabled")
    assert not env.http.calls


async def test_image_allowed_directory_does_not_authorize_video_source(env):

    reference = env.runtime.allowed_roots[0] / "source.mp4"
    reference.write_bytes(b"irrelevant content: local access must be rejected first")
    with pytest.raises(UnsafeMediaSource):
        await env.provider.import_video_source(str(reference), event=env.event)
    capabilities = await env.provider.get_video_capabilities(event=env.event)
    assert (
        "local_file"
        not in capabilities["mode_constraints"]["external_video_import"]["source_types"]
    )
    assert not env.http.calls


async def test_capability_probe_does_not_refresh_token(env):
    env.oauth.status = "authorized"

    async def forbidden_token():
        raise AssertionError("capability must not refresh or request credentials")

    env.oauth.get_token = forbidden_token
    capabilities = await env.provider.get_video_capabilities(event=env.event)
    assert capabilities["available"] and not env.http.calls


async def test_text_job_cannot_cross_conversation_or_account(env):
    from dataclasses import replace

    from astrbot_plugin_grok_oauth.grok_oauth.errors import AssetNotFound, AuthorizationChanged

    job = await env.provider.submit_text_video(
        "prompt", event=env.event, operation_key="scoped-text"
    )
    env.conversation.value = "different-conversation"
    with pytest.raises(AssetNotFound):
        await env.provider.get_video_job(job["job_id"], event=env.event)
    env.conversation.value = "conversation-1"
    env.oauth.token = replace(env.oauth.token, user_id="other-account", epoch=2)
    with pytest.raises(AuthorizationChanged):
        await env.provider.get_video_job(job["job_id"], event=env.event)
    assert len(env.http.calls) == 1


@pytest.mark.parametrize(
    "mode", ["submit_text_video", "submit_video", "edit_video", "edit_video_with_images"]
)
async def test_closed_provider_rejects_each_submission_as_not_submitted(env, mode):
    from astrbot_plugin_grok_oauth.grok_oauth.errors import ServiceClosed

    env.provider._closed = True
    args = ["prompt"]
    if mode in {"submit_video", "edit_video", "edit_video_with_images"}:
        args.append("source")
    if mode == "edit_video_with_images":
        args.append(["identity"])
    with pytest.raises(ServiceClosed) as caught:
        await getattr(env.provider, mode)(*args, event=env.event, operation_key="closed")
    assert caught.value.summary()["submission_state"] == "not_submitted"
    assert not env.http.calls


async def test_disabled_joint_edit_preflight_is_explicitly_not_submitted(env):
    env.runtime.config["videos_enabled"] = False
    with pytest.raises(PermissionDenied) as caught:
        await env.provider.edit_video_with_images(
            "prompt", "source", ["identity"], event=env.event, operation_key="disabled-joint"
        )
    assert caught.value.summary()["submission_state"] == "not_submitted"
    assert not env.http.calls


def source_video_uri():
    import base64

    def box(kind, content):
        return (len(content) + 8).to_bytes(4, "big") + kind + content

    timeline = bytes(12) + (1000).to_bytes(4, "big") + (6000).to_bytes(4, "big")
    track = box(b"tkhd", bytes(20) + (6000).to_bytes(4, "big")) + box(
        b"mdia", box(b"mdhd", timeline) + box(b"hdlr", bytes(8) + b"vide")
    )
    content = (
        box(b"ftyp", b"isom" + bytes(4) + b"mp42")
        + box(b"moov", box(b"mvhd", timeline) + box(b"trak", track))
        + box(b"mdat", bytes(24))
    )
    return "data:video/mp4;base64," + base64.b64encode(content).decode()


async def test_public_external_import_edit_preserves_source_and_prompt(env):
    reference = source_video_uri()
    asset_id = await env.provider.import_video_source(reference, event=env.event)
    assert not env.http.calls
    prompt = "  中文编辑要求。\n Preserve the original action.  "
    job = await env.provider.edit_video(
        prompt, asset_id, event=env.event, operation_key="external-edit"
    )
    assert job["status"] == "pending" and job["submission_state"] == "submitted"
    assert env.http.calls == [
        (
            "POST",
            "/videos/edits",
            {"model": "grok-imagine-video", "prompt": prompt, "video": {"url": reference}},
        )
    ]
    done = await env.provider.wait_video_job(job["job_id"], event=env.event)
    assert done["status"] == "done" and done["delivery_owner"] == "caller"


async def test_imported_source_cannot_cross_group_or_account_before_edit(env):
    from dataclasses import replace

    from astrbot_plugin_grok_oauth.grok_oauth.errors import AssetNotFound, AuthorizationChanged

    asset_id = await env.provider.import_video_source(source_video_uri(), event=env.event)
    env.event.unified_msg_origin = "qq:GroupMessage:456"
    with pytest.raises(AssetNotFound) as caught:
        await env.provider.edit_video(
            "prompt", asset_id, event=env.event, operation_key="cross-group"
        )
    assert caught.value.summary()["submission_state"] == "not_submitted"
    env.event.unified_msg_origin = "qq:GroupMessage:123"
    env.oauth.token = replace(env.oauth.token, user_id="other-account", epoch=2)
    with pytest.raises(AuthorizationChanged) as caught:
        await env.provider.edit_video(
            "prompt", asset_id, event=env.event, operation_key="cross-account"
        )
    assert caught.value.summary()["submission_state"] == "not_submitted"
    assert not env.http.calls


async def test_import_rechecks_binding_after_materializing_bytes(env):
    from dataclasses import replace

    from astrbot_plugin_grok_oauth.grok_oauth.errors import AuthorizationChanged

    real_import = env.store.import_reference

    async def rebind_after_import(*args, **kwargs):
        asset_id = await real_import(*args, **kwargs)
        env.oauth.token = replace(env.oauth.token, user_id="other-account", epoch=2)
        return asset_id

    env.store.import_reference = rebind_after_import
    with pytest.raises(AuthorizationChanged):
        await env.provider.import_video_source(source_video_uri(), event=env.event)
    assert not env.http.calls


async def test_sdk_unknown_backend_failure_is_never_defaulted_to_not_submitted(env):
    from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError

    async def backend_failure(*args, **kwargs):
        raise ProtocolError("backend stage cannot be recovered")

    env.service.submit = backend_failure
    with pytest.raises(ProtocolError) as caught:
        await env.provider.submit_text_video(
            "prompt", event=env.event, operation_key="backend-error"
        )
    assert caught.value.summary()["submission_state"] == "unknown"


async def test_edit_capabilities_expose_actual_inherited_constraints(env):
    capabilities = await env.provider.get_video_capabilities(event=env.event)
    edit = capabilities["mode_constraints"]["video_edit"]
    assert edit["max_output_resolution"] == "720p"
    assert edit["preserves_source_duration"] is True
    assert edit["preserves_source_aspect_ratio"] is True
