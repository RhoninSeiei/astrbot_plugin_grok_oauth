from datetime import UTC, datetime

import pytest

from grok_oauth.billing_parser import QuotaSnapshot, parse_billing

OBSERVED = datetime(2026, 9, 20, 1, 2, 3, tzinfo=UTC)
EXPECTED_KEYS = {
    "status",
    "scope",
    "period_type",
    "used_percent",
    "remaining_percent",
    "period_start",
    "reset_at",
    "reset_at_local",
    "observed_at",
    "cached",
    "stale",
    "source",
}


def parse_config(config, observed_at=OBSERVED):
    return parse_billing({"config": config}, observed_at)


def test_new_weekly_shape_returns_only_normalized_quota_fields():
    snapshot = parse_config(
        {
            "creditUsagePercent": 42.5,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-09-14T19:30:00-04:00",
                "end": "2026-09-21T00:00:00Z",
            },
            "isUnifiedBillingUser": True,
            "subscriptionTier": "must-not-leak",
            "prepaidBalance": {"val": 1250},
        }
    )

    assert isinstance(snapshot, QuotaSnapshot)
    assert snapshot.to_dict() == {
        "status": "success",
        "scope": "account_shared",
        "period_type": "weekly",
        "used_percent": 42.5,
        "remaining_percent": 57.5,
        "period_start": "2026-09-14T23:30:00Z",
        "reset_at": "2026-09-21T00:00:00Z",
        "reset_at_local": "2026-09-21T08:00:00+08:00",
        "observed_at": "2026-09-20T01:02:03Z",
        "cached": False,
        "stale": False,
        "source": "grok_oauth_billing",
    }
    assert set(snapshot.to_dict()) == EXPECTED_KEYS


def test_snapshot_is_immutable_and_supports_empty_construction():
    snapshot = QuotaSnapshot(status="unknown", observed_at="2026-09-20T01:02:03Z")

    assert snapshot.used_percent is None
    assert snapshot.scope == "unknown"
    with pytest.raises((AttributeError, TypeError)):
        snapshot.status = "success"


@pytest.mark.parametrize(
    ("used", "remaining"),
    [(0, 100), (100, 0), (12.25, 87.75)],
)
def test_new_percentage_accepts_inclusive_finite_bounds(used, remaining):
    snapshot = parse_config(
        {
            "creditUsagePercent": used,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_MONTHLY",
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-10-01T00:00:00Z",
            },
        }
    )

    assert snapshot.status == "success"
    assert snapshot.period_type == "monthly"
    assert snapshot.used_percent == used
    assert snapshot.remaining_percent == remaining
    assert snapshot.scope == "unknown"


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        "42",
        -0.01,
        100.01,
        10**1000,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_invalid_new_percentage_is_unparseable_and_never_falls_back(value):
    snapshot = parse_config(
        {
            "creditUsagePercent": value,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-09-14T00:00:00Z",
                "end": "2026-09-21T00:00:00Z",
            },
            "monthlyLimit": {"val": 200},
            "used": {"val": 50},
            "billingPeriodStart": "2026-09-01T00:00:00Z",
            "billingPeriodEnd": "2026-10-01T00:00:00Z",
        }
    )

    assert snapshot.status == "unparseable"
    assert snapshot.used_percent is None
    assert snapshot.remaining_percent is None


def test_missing_percentage_is_unknown_instead_of_zero():
    snapshot = parse_config(
        {
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-09-14T00:00:00Z",
                "end": "2026-09-21T00:00:00Z",
            }
        }
    )

    assert snapshot.status == "unknown"
    assert snapshot.used_percent is None
    assert snapshot.remaining_percent is None
    assert snapshot.period_start == "2026-09-14T00:00:00Z"


def test_new_percentage_without_period_is_preserved_as_incomplete_information():
    snapshot = parse_config({"creditUsagePercent": 12})

    assert snapshot.status == "unknown"
    assert snapshot.period_type == "unknown"
    assert snapshot.used_percent == 12
    assert snapshot.remaining_percent == 88
    assert snapshot.period_start is None
    assert snapshot.reset_at is None
    assert snapshot.reset_at_local is None


def test_legacy_same_unit_values_require_complete_period():
    snapshot = parse_config(
        {
            "monthlyLimit": {"val": 250},
            "used": {"val": 50},
            "billingPeriodStart": "2026-09-01T00:00:00Z",
            "billingPeriodEnd": "2026-10-01T00:00:00Z",
            "isUnifiedBillingUser": False,
        }
    )

    assert snapshot.status == "success"
    assert snapshot.scope == "unknown"
    assert snapshot.period_type == "monthly"
    assert snapshot.used_percent == 20
    assert snapshot.remaining_percent == 80


@pytest.mark.parametrize(
    ("limit", "used"),
    [
        ({}, {"val": 0}),
        ({"val": 0}, {"val": 0}),
        ({"val": -1}, {"val": 0}),
        ({"val": 100}, {}),
        ({"val": 100}, {"val": True}),
        ({"val": 100}, {"val": "25"}),
        ({"val": 100}, {"val": 101}),
    ],
)
def test_legacy_values_do_not_infer_omitted_zero_or_invalid_ratio(limit, used):
    snapshot = parse_config(
        {
            "monthlyLimit": limit,
            "used": used,
            "billingPeriodStart": "2026-09-01T00:00:00Z",
            "billingPeriodEnd": "2026-10-01T00:00:00Z",
        }
    )

    assert snapshot.status == "unparseable"
    assert snapshot.used_percent is None
    assert snapshot.remaining_percent is None


def test_legacy_ratio_without_complete_period_is_unparseable():
    snapshot = parse_config({"monthlyLimit": {"val": 100}, "used": {"val": 25}})

    assert snapshot.status == "unparseable"
    assert snapshot.period_start is None
    assert snapshot.reset_at is None


def test_unknown_period_enum_stays_unknown_without_guessing():
    snapshot = parse_config(
        {
            "creditUsagePercent": 10,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_DAILY",
                "start": "2026-09-20T00:00:00Z",
                "end": "2026-09-21T00:00:00Z",
            },
        }
    )

    assert snapshot.status == "success"
    assert snapshot.period_type == "unknown"
    assert snapshot.reset_at == "2026-09-21T00:00:00Z"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, "2026-09-21T00:00:00Z"),
        ("2026-09-20T00:00:00Z", None),
        ("2026-09-20", "2026-09-21T00:00:00Z"),
        ("2026-09-20T00:00:00", "2026-09-21T00:00:00Z"),
        ("private-invalid-value", "2026-09-21T00:00:00Z"),
        ("2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z"),
        ("2026-09-22T00:00:00Z", "2026-09-21T00:00:00Z"),
    ],
)
def test_invalid_current_period_is_unparseable_without_raw_time_leak(start, end):
    snapshot = parse_config(
        {
            "creditUsagePercent": 10,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": start,
                "end": end,
            },
        }
    )

    assert snapshot.status == "unparseable"
    assert snapshot.period_start is None
    assert snapshot.reset_at is None
    assert snapshot.reset_at_local is None
    assert "private-invalid-value" not in repr(snapshot)
    assert "private-invalid-value" not in str(snapshot.to_dict())


@pytest.mark.parametrize("data", [{}, {"config": None}, {"config": {}}])
def test_absent_config_is_unknown_with_fixed_metadata(data):
    snapshot = parse_billing(data, 1_779_235_323)

    assert snapshot.status == "unknown"
    assert snapshot.observed_at == "2026-05-20T00:02:03Z"
    assert snapshot.cached is False
    assert snapshot.stale is False
    assert snapshot.source == "grok_oauth_billing"
    assert snapshot.used_percent is None


@pytest.mark.parametrize(
    "observed_at",
    [True, "2026-09-20T01:02:03Z", float("nan"), 10**1000],
)
def test_observed_at_rejects_ambiguous_or_nonfinite_values(observed_at):
    with pytest.raises((TypeError, ValueError)):
        parse_billing({}, observed_at)


def test_large_finite_legacy_values_do_not_overflow_percentage():
    result = parse_config(
        {
            "monthlyLimit": {"val": 1e308},
            "used": {"val": 1e308},
            "billingPeriodStart": "2026-09-01T00:00:00Z",
            "billingPeriodEnd": "2026-10-01T00:00:00Z",
        }
    )
    assert result.used_percent == 100 and result.remaining_percent == 0


@pytest.mark.parametrize(
    "start,end",
    [
        ("0001-01-01T00:00:00+08:00", "2026-09-26T00:00:00Z"),
        ("2026-09-19T00:00:00Z", "9999-12-31T23:59:59Z"),
    ],
)
def test_calendar_overflow_is_unparseable(start, end):
    result = parse_config({"creditUsagePercent": 20, "currentPeriod": {"start": start, "end": end}})
    assert result.status == "unparseable" and result.used_percent is None


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("2026-09-22T14:14:46Z", "unknown"),
        ("2026-09-22T14:14:47Z", "success"),
        ("2026-09-23T02:00:00Z", "success"),
        ("2026-09-29T14:14:47Z", "unknown"),
    ],
)
def test_shared_weekly_omitted_zero_only_within_current_period(observed, expected):
    config = {
        "isUnifiedBillingUser": True,
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-09-22T14:14:47Z",
            "end": "2026-09-29T14:14:47Z",
        },
    }
    result = parse_config(config, datetime.fromisoformat(observed))
    assert result.status == expected
    assert result.used_percent == (0 if expected == "success" else None)
    assert result.remaining_percent == (100 if expected == "success" else None)


@pytest.mark.parametrize(
    "change",
    [
        {"creditUsagePercent": None},
        {"creditUsagePercent": -1},
        {"used": {}},
        {"monthlyLimit": {}},
    ],
)
def test_omitted_zero_never_overrides_explicit_invalid_percentage(change):
    config = {
        "isUnifiedBillingUser": True,
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-09-14T00:00:00Z",
            "end": "2026-09-21T00:00:00Z",
        },
        **change,
    }
    result = parse_config(config)
    assert result.status == "unparseable"
    assert result.used_percent is None and result.remaining_percent is None


@pytest.mark.parametrize("period_type", ["USAGE_PERIOD_TYPE_MONTHLY", "future_period"])
def test_omitted_zero_does_not_apply_to_other_period_types(period_type):
    result = parse_config(
        {
            "isUnifiedBillingUser": True,
            "currentPeriod": {
                "type": period_type,
                "start": "2026-09-14T00:00:00Z",
                "end": "2026-09-21T00:00:00Z",
            },
        }
    )
    assert result.status == "unknown"
    assert result.used_percent is None and result.remaining_percent is None
