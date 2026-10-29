import json

import pytest

from grok_oauth.diagnostic_fields import (
    DIAGNOSTIC_ERROR_CODES,
    DIAGNOSTIC_ERROR_PARAMS,
    request_kind,
    safe_error_metadata,
)


@pytest.mark.parametrize(
    "payload,code,param",
    [
        (
            {"error": {"code": "invalid_argument", "param": "tools", "message": "PRIVATE"}},
            "invalid_argument",
            "tools",
        ),
        (
            {"error": {"type": "invalid_request_error", "param": "input"}},
            "invalid_request_error",
            "input",
        ),
        (
            {"code": "context_length_exceeded", "param": "max_output_tokens", "message": "PRIVATE"},
            "context_length_exceeded",
            "max_output_tokens",
        ),
        ({"type": "invalid_image", "param": "image"}, "invalid_image", "image"),
        ({"error": "client_not_eligible"}, "client_not_eligible", ""),
        ({"error": {"code": None, "type": "server_error"}}, "server_error", ""),
        ({"code": "invalid_client", "error": "PRIVATE"}, "invalid_client", ""),
        ({}, "", ""),
    ],
)
def test_error_metadata_keeps_only_known_codes_and_parameters(payload, code, param):
    result = safe_error_metadata(json.dumps(payload).encode())
    assert result == {"upstream_error_code": code, "upstream_error_param": param}
    assert result["upstream_error_code"] in DIAGNOSTIC_ERROR_CODES
    assert result["upstream_error_param"] in DIAGNOSTIC_ERROR_PARAMS
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "code,param",
    [
        ("PRIVATE", "PRIVATE"),
        ("https://api.x.ai/?token=PRIVATE", "input[0].PRIVATE"),
        ("eyJ.private.token", "Bearer PRIVATE"),
        ({"token": "PRIVATE"}, ["PRIVATE"]),
        (42, True),
        ("server_error_PRIVATE", "prompt_PRIVATE"),
    ],
)
def test_unrecognized_error_metadata_is_fixed_unknown(code, param):
    result = safe_error_metadata(json.dumps({"error": {"code": code, "param": param}}).encode())
    assert result == {"upstream_error_code": "unknown", "upstream_error_param": "unknown"}
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [b"", b"not json PRIVATE", b"\xff", b"[]", b"null", b'"PRIVATE"', b" " * (64 * 1024 + 1)],
)
def test_empty_invalid_and_oversize_error_bodies_never_escape(body):
    assert safe_error_metadata(body) == {"upstream_error_code": "", "upstream_error_param": ""}


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("POST", "/responses", "chat"),
        ("POST", "/images/generations", "image_generate"),
        ("POST", "/images/edits", "image_edit"),
        ("POST", "/videos/generations", "video_generate"),
        ("POST", "/videos/edits", "video_edit"),
        ("GET", "/videos/PRIVATE-identity_123", "video_status"),
        ("GET", "/models", "models"),
        ("get", "/models", "models"),
        ("GET", "/responses", "other"),
        ("POST", "/unknown/PRIVATE", "other"),
        ("POST", "https://api.x.ai/v1/responses?token=PRIVATE", "other"),
        ("POST", "/responses?token=PRIVATE", "other"),
        ("GET", "/videos/PRIVATE?token=PRIVATE", "other"),
        ("POST", "/images/edits#PRIVATE", "other"),
        ("GET", "/videos/PRIVATE/segment", "other"),
        ("GET", "/videos/" + "x" * 129, "other"),
        (None, "/models", "other"),
        ("POST", None, "other"),
    ],
)
def test_request_kind_is_fixed_and_never_echoes_urls_or_job_ids(method, path, expected):
    assert request_kind(method, path) == expected
    assert "PRIVATE" not in request_kind(method, path)
