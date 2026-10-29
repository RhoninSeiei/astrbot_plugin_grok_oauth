from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from grok_oauth.billing_parser import parse_billing

OBSERVED = datetime(2026, 9, 20, 6, 7, 8, tzinfo=UTC)
PERIOD = {
    "type": "USAGE_PERIOD_TYPE_WEEKLY",
    "start": "2026-09-14T00:00:00Z",
    "end": "2026-09-21T00:00:00Z",
}
QUOTA_KEYS = {
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
BREAKDOWN_KEYS = {
    "status",
    "scope",
    "period_type",
    "period_start",
    "reset_at",
    "reset_at_local",
    "observed_at",
    "cached",
    "stale",
    "source",
    "products",
    "unrecognized_product_count",
    "percent_basis",
}


def parse_config(config):
    return parse_billing({"config": config}, OBSERVED)


def quota_config(product_usage):
    return {
        "creditUsagePercent": 27,
        "currentPeriod": PERIOD,
        "isUnifiedBillingUser": True,
        "productUsage": product_usage,
        "prepaidBalance": {"val": 12345},
        "accountId": "must-not-leak",
    }


def test_three_confirmed_products_have_independent_allowlisted_output():
    snapshot = parse_config(
        quota_config(
            [
                {"product": "GrokBuild", "usagePercent": 11.5},
                {"product": "GrokChat", "usagePercent": 22},
                {"product": "GrokImagine", "usagePercent": 33.25},
            ]
        )
    )

    quota = snapshot.to_dict()
    breakdown = snapshot.to_breakdown_dict()
    assert set(quota) == QUOTA_KEYS
    assert quota["status"] == "success"
    assert quota["used_percent"] == 27
    assert "products" not in quota
    assert breakdown == {
        "status": "success",
        "scope": "account_shared",
        "period_type": "weekly",
        "period_start": "2026-09-14T00:00:00Z",
        "reset_at": "2026-09-21T00:00:00Z",
        "reset_at_local": "2026-09-21T08:00:00+08:00",
        "observed_at": "2026-09-20T06:07:08Z",
        "cached": False,
        "stale": False,
        "source": "grok_oauth_billing",
        "products": [
            {
                "display_name": "Grok Build",
                "product": "GrokBuild",
                "usage_percent": 11.5,
            },
            {"display_name": "Grok", "product": "GrokChat", "usage_percent": 22.0},
            {
                "display_name": "Imagine",
                "product": "GrokImagine",
                "usage_percent": 33.25,
            },
        ],
        "unrecognized_product_count": 0,
        "percent_basis": "upstream_product_usage",
    }
    assert set(breakdown) == BREAKDOWN_KEYS
    assert "prepaidBalance" not in str(breakdown)
    assert "must-not-leak" not in str(breakdown)


def test_legacy_build_name_maps_to_canonical_product_and_bounds_are_preserved():
    snapshot = parse_config(
        quota_config(
            [
                {"product": "PRODUCT_GROK_BUILD", "usagePercent": 0},
                {"product": "GrokImagine", "usagePercent": 100},
            ]
        )
    )

    assert snapshot.to_breakdown_dict()["products"] == [
        {"display_name": "Grok Build", "product": "GrokBuild", "usage_percent": 0.0},
        {
            "display_name": "Imagine",
            "product": "GrokImagine",
            "usage_percent": 100.0,
        },
    ]


@pytest.mark.parametrize(
    "product_usage",
    [
        {},
        "not-a-list",
        ["not-an-object"],
        [{"product": 3, "usagePercent": 10}],
        [
            {"product": "GrokBuild", "usagePercent": 10},
            {"product": "PRODUCT_GROK_BUILD", "usagePercent": 20},
        ],
        [{"product": "GrokBuild", "usagePercent": True}],
        [{"product": "GrokBuild", "usagePercent": "10"}],
        [{"product": "GrokBuild", "usagePercent": -0.1}],
        [{"product": "GrokBuild", "usagePercent": 100.1}],
        [{"product": "GrokBuild", "usagePercent": float("nan")}],
        [{"product": "GrokBuild", "usagePercent": float("inf")}],
        [{"product": "UNKNOWN_FUTURE_PRODUCT", "usagePercent": "10"}],
        [
            {"product": "GrokChat", "usagePercent": 10},
            {"product": "UNKNOWN_FUTURE_PRODUCT", "usagePercent": 101},
        ],
    ],
)
def test_malformed_product_usage_does_not_change_valid_total(product_usage):
    snapshot = parse_config(quota_config(product_usage))

    assert snapshot.to_dict()["status"] == "success"
    assert snapshot.to_dict()["used_percent"] == 27
    assert snapshot.to_breakdown_dict()["status"] == "unparseable"
    assert snapshot.to_breakdown_dict()["products"] == []


def test_valid_breakdown_survives_unparseable_total_quota():
    snapshot = parse_config(
        {
            "creditUsagePercent": "invalid-total",
            "currentPeriod": PERIOD,
            "productUsage": [{"product": "GrokChat", "usagePercent": 41}],
        }
    )

    assert snapshot.to_dict()["status"] == "unparseable"
    assert snapshot.to_dict()["used_percent"] is None
    assert snapshot.to_breakdown_dict()["status"] == "success"
    assert snapshot.to_breakdown_dict()["products"][0]["usage_percent"] == 41


@pytest.mark.parametrize("product_usage", [None, []])
def test_missing_or_empty_product_usage_is_unknown(product_usage):
    config = quota_config([])
    if product_usage is None:
        config.pop("productUsage")
    else:
        config["productUsage"] = product_usage

    breakdown = parse_config(config).to_breakdown_dict()
    assert breakdown["status"] == "unknown"
    assert breakdown["products"] == []
    assert breakdown["unrecognized_product_count"] == 0


def test_missing_known_percentage_is_null_and_status_reflects_completeness():
    only_missing = parse_config(quota_config([{"product": "GrokBuild"}])).to_breakdown_dict()
    partial = parse_config(
        quota_config(
            [
                {"product": "GrokBuild"},
                {"product": "GrokChat", "usagePercent": 9},
            ]
        )
    ).to_breakdown_dict()

    assert only_missing["status"] == "unknown"
    assert only_missing["products"][0]["usage_percent"] is None
    assert partial["status"] == "partial"
    assert partial["products"][0]["usage_percent"] is None
    assert partial["products"][1]["usage_percent"] == 9


def test_unknown_products_are_counted_without_returning_their_names():
    unknown_name = "PRIVATE_FUTURE_PRODUCT_ACCOUNT_123"
    unknown_only = parse_config(
        quota_config([{"product": unknown_name, "usagePercent": 50}])
    ).to_breakdown_dict()
    partial = parse_config(
        quota_config(
            [
                {"product": unknown_name, "usagePercent": 50},
                {"product": "GrokChat", "usagePercent": 5},
            ]
        )
    ).to_breakdown_dict()

    assert unknown_only["status"] == "unknown"
    assert unknown_only["unrecognized_product_count"] == 1
    assert unknown_only["products"] == []
    assert partial["status"] == "partial"
    assert partial["unrecognized_product_count"] == 1
    assert unknown_name not in str(unknown_only)
    assert unknown_name not in str(partial)


def test_product_usage_is_bounded_to_32_rows():
    rows = [{"product": f"UNKNOWN_{index}", "usagePercent": 1} for index in range(33)]

    breakdown = parse_config(quota_config(rows)).to_breakdown_dict()
    assert breakdown["status"] == "unparseable"
    assert breakdown["products"] == []
    assert breakdown["unrecognized_product_count"] == 0


def test_products_are_immutable_and_replace_preserves_breakdown_metadata():
    snapshot = parse_config(quota_config([{"product": "GrokBuild", "usagePercent": 12}]))

    assert isinstance(snapshot.products, tuple)
    with pytest.raises(FrozenInstanceError):
        snapshot.products[0].usage_percent = 13

    cached = replace(snapshot, cached=True, stale=True)
    assert cached.products is snapshot.products
    assert cached.to_breakdown_dict()["cached"] is True
    assert cached.to_breakdown_dict()["stale"] is True
    assert cached.to_breakdown_dict()["status"] == "success"


@pytest.mark.parametrize("transport_status", ["unavailable", "rate_limited", "forbidden"])
def test_transport_error_status_overrides_breakdown_parse_status(transport_status):
    snapshot = parse_config(quota_config([{"product": "GrokBuild", "usagePercent": 12}]))

    failed = replace(snapshot, status=transport_status, stale=True)
    assert failed.to_breakdown_dict()["status"] == transport_status
    assert failed.to_breakdown_dict()["products"][0]["usage_percent"] == 12
