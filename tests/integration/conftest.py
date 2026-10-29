# Real host imports must fail collection instead of silently skipping tests.
import os
from contextvars import ContextVar
from pathlib import Path

import astrbot
import pytest

expected_source = Path(os.environ.get("ASTRBOT_SOURCE", "/AstrBot")).resolve()
assert Path(astrbot.__file__).resolve().is_relative_to(expected_source), (
    "Integration must use the explicitly selected AstrBot source"
)


@pytest.fixture
def host_search_policy(monkeypatch):
    """Supply the optional host extension without skipping stock-core tests."""
    from astrbot.core.provider.sources import request_retry

    policy = ContextVar("grok_test_host_search_policy", default="inherit")
    monkeypatch.setattr(request_retry, "provider_oauth_web_search", policy, raising=False)
    return policy
