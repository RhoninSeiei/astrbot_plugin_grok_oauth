"""Allowlisted usage counters actually returned by a response, without estimates."""


def _counter(value):
    return type(value) is int and value >= 0


def safe_token_usage(raw):
    """Copy valid reported counters and derive completeness from their presence.

    Missing directions stay absent. Cached tokens are a subset of input tokens,
    and an upstream total is retained only when both directions corroborate it.
    Unknown metadata never crosses this boundary.
    """
    if not isinstance(raw, dict):
        return None
    usage = {
        name: raw[name]
        for name in ("input_tokens", "output_tokens")
        if name in raw and _counter(raw[name])
    }
    partial = len(usage) != 2
    details = raw.get("input_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if _counter(cached) and ("input_tokens" not in usage or cached <= usage["input_tokens"]):
            usage["input_tokens_details"] = {"cached_tokens": cached}
        elif cached is not None:
            partial = True
    elif details is not None:
        partial = True
    if "total_tokens" in raw:
        total = raw["total_tokens"]
        if (
            "input_tokens" in usage
            and "output_tokens" in usage
            and _counter(total)
            and total == usage["input_tokens"] + usage["output_tokens"]
        ):
            usage["total_tokens"] = total
        else:
            partial = True
    if not usage:
        return None
    if partial:
        usage["is_partial"] = True
    return usage
