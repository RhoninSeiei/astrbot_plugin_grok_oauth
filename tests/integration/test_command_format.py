import os
import time

import pytest
from astrbot_plugin_grok_oauth.astrbot_adapter import usage_tools


@pytest.fixture
def local_zone():
    previous = os.environ.get("TZ")
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.mark.parametrize(
    "zone,reset,observed",
    [
        ("Asia/Shanghai", "2026-09-26 08:00:00 UTC+08:00", "2026-09-20 17:00:00 UTC+08:00"),
        ("UTC", "2026-09-26 00:00:00 UTC+00:00", "2026-09-20 09:00:00 UTC+00:00"),
        ("America/New_York", "2026-09-25 20:00:00 UTC-04:00", "2026-09-20 05:00:00 UTC-04:00"),
    ],
)
def test_both_commands_use_machine_timezone(local_zone, zone, reset, observed):
    os.environ["TZ"] = zone
    time.tzset()
    data = {
        "status": "success",
        "reset_at": "2026-09-26T00:00:00Z",
        "reset_at_local": "wrong hardcoded time",
        "observed_at": "2026-09-20T09:00:00Z",
        "products": [],
    }
    for formatter in [usage_tools.format_usage, usage_tools.format_usage_breakdown]:
        result = formatter(data)
        assert "重置时间：" + reset in result
        assert "采集时间：" + observed in result
        assert "wrong" not in result and "北京时间" not in result


def test_breakdown_unknown_zero_partial_stale():
    result = usage_tools.format_usage_breakdown(
        {
            "status": "partial",
            "stale": True,
            "cached": True,
            "products": [
                {"display_name": "Grok", "usage_percent": 0},
                {"display_name": "Imagine", "usage_percent": None},
            ],
            "unrecognized_product_count": 1,
        }
    )
    assert "Grok：0%" in result and "Imagine：未知" in result
    assert "历史快照" in result and "缓存" in result and "1" in result
    assert "上游" in result


@pytest.mark.parametrize("value", [None, "bad", "2026-09-20T09:00:00"])
def test_invalid_time_is_unknown(value):
    result = usage_tools.format_usage(
        {"status": "unknown", "reset_at": value, "observed_at": value}
    )
    assert "重置时间：未知" in result and "采集时间：未知" in result


def test_local_conversion_uses_offset_at_each_instant(local_zone):
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    result = usage_tools.format_usage(
        {
            "status": "success",
            "reset_at": "2026-12-01T00:00:00Z",
            "observed_at": "2026-09-20T09:00:00Z",
        }
    )
    assert "2026-11-30 19:00:00 UTC-05:00" in result
    assert "2026-09-20 05:00:00 UTC-04:00" in result
