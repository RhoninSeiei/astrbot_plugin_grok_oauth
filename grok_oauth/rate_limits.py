"""Conservative decoding of explicit upstream 429 evidence."""

import re
from email.utils import format_datetime, parsedate_to_datetime

from .diagnostic_fields import DIAGNOSTIC_ERROR_CODES

_BUCKETS = ("requests", "tokens", "concurrency")
_SCOPES = {"account", "model", "team", "organization", "project", "user", "key", "global"}
_UNITS = {"rps", "rpm", "tpm", "requests", "tokens", "concurrency"}
MAX_COUNT = 10**12
MAX_DELAY = 366 * 86400
MAX_EPOCH = 253402300799  # 9999-12-31T23:59:59Z
ENUM_FIELDS = {
    "upstream_error_type": DIAGNOSTIC_ERROR_CODES,
    "body_scope": _SCOPES,
    "body_unit": _UNITS,
    "header_scope": _SCOPES,
    "header_unit": _UNITS,
}
# (maximum, allow fractional seconds). Both diagnostic sinks validate again.
NUMBER_FIELDS = {
    "upstream_retry_after_seconds": (MAX_DELAY, True),
    "retry_after_seconds": (MAX_DELAY, True),
    "template_rpm_actual": (MAX_COUNT, False),
    "template_rpm_limit": (MAX_COUNT, False),
    "body_window_seconds": (MAX_DELAY, False),
    "body_reset_at_unix": (MAX_EPOCH, False),
    "body_reset_after_seconds": (MAX_DELAY, False),
    **{f"body_{key}": (MAX_COUNT, False) for key in ("limit", "remaining")},
}
for bucket in _BUCKETS:
    for key, spec in (
        ("limit", (MAX_COUNT, False)),
        ("remaining", (MAX_COUNT, False)),
        ("reset_after_seconds", (MAX_DELAY, True)),
        ("reset_at_unix", (MAX_EPOCH, False)),
    ):
        NUMBER_FIELDS[f"header_{bucket}_{key}"] = spec


def _uint(value, maximum):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,13}", value):
        value = int(value)
    return value if type(value) is int and 0 <= value <= maximum else None


def _one(headers, key):
    values = headers.get_list(key)
    if len(values) == 1 and len(values[0]) <= 96:
        return values[0].strip()
    return None


def _date(value):
    # Canonical HTTP date only; permissive parsers can accept trailing junk.
    if not value or len(value) != 29 or not value.endswith(" GMT"):
        return None
    try:
        dt = parsedate_to_datetime(value)
        if format_datetime(dt, usegmt=True) == value:
            return _uint(int(dt.timestamp()), MAX_EPOCH)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return None


def retry_delay(headers, now):
    value = _one(headers, "retry-after")
    if value and re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,6})?", value):
        # Keep compatibility with upstreams that previously sent fractional seconds.
        seconds = float(value)
    else:
        epoch = _date(value)
        if epoch is None:
            return None
        seconds = max(0, epoch - now)
    return seconds if seconds <= MAX_DELAY else None


def _duration(value):
    # An unqualified reset number has ambiguous units and epoch.
    if value:
        m = re.fullmatch(r"([0-9]{1,12}(?:\.[0-9]{1,6})?)(ms|s|m|h)", value)
        if m:
            seconds = float(m[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[m[2]]
            return seconds if seconds <= MAX_DELAY else None
    return None


def header_metadata(headers):
    result = {}
    for key in ("scope", "unit"):
        value = _one(headers, f"x-ratelimit-{key}")
        if value in ENUM_FIELDS[f"header_{key}"]:
            result[f"header_{key}"] = value
    for bucket in _BUCKETS:
        for key in ("limit", "remaining"):
            value = _uint(_one(headers, f"x-ratelimit-{key}-{bucket}"), MAX_COUNT)
            if value is not None:
                result[f"header_{bucket}_{key}"] = value
        raw = _one(headers, f"x-ratelimit-reset-{bucket}")
        delay, epoch = _duration(raw), _date(raw)
        if delay is not None:
            result[f"header_{bucket}_reset_after_seconds"] = delay
        elif epoch is not None:
            result[f"header_{bucket}_reset_at_unix"] = epoch
    return result


def body_metadata(payload):
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    inner = error if isinstance(error, dict) else {}
    category = inner.get("type", payload.get("type"))
    result = {}
    if isinstance(category, str) and category:
        result["upstream_error_type"] = (
            category if category in DIAGNOSTIC_ERROR_CODES else "unknown"
        )
    # A historical xAI response used this exact labelled actual/limit pair.
    # Accept a complete delimited clause only; never forward the message.
    message = error if isinstance(error, str) else inner.get("message", payload.get("message"))
    if isinstance(message, str):
        matches = re.findall(
            r"(?:^|[.;\n])\s*Requests per Minute \(actual/limit\): ([0-9]{1,13})/([0-9]{1,13})(?=\s*(?:[.;\n]|$))",
            message,
        )
        if len(matches) == 1:
            actual, limit = (_uint(x, MAX_COUNT) for x in matches[0])
            if actual is not None and limit is not None:
                result.update(template_rpm_actual=actual, template_rpm_limit=limit)
    detail = inner.get("rate_limit", payload.get("rate_limit"))
    if isinstance(detail, dict):
        for field in ("scope", "unit"):
            value = detail.get(field)
            if isinstance(value, str) and value in ENUM_FIELDS[f"body_{field}"]:
                result[f"body_{field}"] = value
        for field in (
            "limit",
            "remaining",
            "window_seconds",
            "reset_at_unix",
            "reset_after_seconds",
        ):
            value = _uint(detail.get(field), NUMBER_FIELDS[f"body_{field}"][0])
            if value is not None:
                result[f"body_{field}"] = value
    return result
