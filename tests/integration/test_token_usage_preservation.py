"""Explicit counters survive adaptation without inventing unreported directions."""

import pytest
from astrbot_plugin_grok_oauth.grok_oauth.errors import ProtocolError
from test_provider import bundle as bundle
from test_provider import provider


@pytest.mark.parametrize(
    "raw,expected,partial",
    [
        ({"input_tokens": 7}, (7, 0, 0), True),
        ({"output_tokens": 4}, (0, 0, 4), True),
        ({"input_tokens": 0}, (0, 0, 0), True),
        ({"output_tokens": 0}, (0, 0, 0), True),
        ({"input_tokens": 0, "output_tokens": 0}, (0, 0, 0), False),
        (
            {"input_tokens": 7, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 3}},
            (4, 3, 4),
            False,
        ),
        ({"input_tokens": 7, "input_tokens_details": {"cached_tokens": 3}}, (4, 3, 0), True),
        ({"output_tokens": 4, "input_tokens_details": {"cached_tokens": 3}}, (0, 0, 4), True),
        ({"input_tokens": 7, "output_tokens": 4, "total_tokens": 1000}, (7, 0, 4), True),
        ({"total_tokens": 9}, None, None),
        ({"input_tokens_details": {"cached_tokens": 3}}, None, None),
        ({}, None, None),
        (None, None, None),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_explicit_counter_presence(bundle, raw, expected, partial, streaming):
    bundle.responses.raw["usage"] = raw
    p = provider(bundle)
    response = (
        [r async for r in p.text_chat_stream(prompt="hi")][-1]
        if streaming
        else await p.text_chat(prompt="hi")
    )
    if expected is None:
        assert response.usage is None
    else:
        assert (
            response.usage.input_other,
            response.usage.input_cached,
            response.usage.output,
        ) == expected
        assert response.usage.total == sum(expected)
        assert response.usage.is_partial is partial
        assert response.raw_completion["usage_status"] == ("partial" if partial else "reported")
        for key in ("input_tokens", "output_tokens"):
            assert (key in response.raw_completion["usage"]) == (key in raw)


async def test_raw_usage_metadata_contains_no_response_or_unknown_usage_fields(bundle):
    bundle.responses.raw["usage"] = {
        "input_tokens": 7,
        "password": "private",
        "input_tokens_details": {"cached_tokens": 2, "prompt": "private"},
    }
    response = await provider(bundle).text_chat(prompt="private")
    assert set(response.raw_completion) == {"usage", "usage_status"}
    assert "private" not in str(response.raw_completion)
    assert response.usage.total == 7


@pytest.mark.parametrize("streaming", [False, True])
async def test_adapter_failure_carries_already_received_usage(bundle, monkeypatch, streaming):
    bundle.responses.raw["usage"] = {"input_tokens": 7, "output_tokens": 2}
    p = provider(bundle)

    def reject(*args, **kwargs):
        raise ProtocolError("adapter rejected response")

    monkeypatch.setattr(p, "_response", reject)
    with pytest.raises(ProtocolError) as caught:
        if streaming:
            [r async for r in p.text_chat_stream(prompt="hi")]
        else:
            await p.text_chat(prompt="hi")
    usage = caught.value._astrbot_token_usage
    assert usage.total == 9
    assert usage.is_partial is False
    assert "private" not in str(caught.value.__dict__)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            {"input_tokens": 3, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 99}},
            (3, 0, 2),
        ),
        ({"input_tokens": True, "output_tokens": 2}, (0, 0, 2)),
        ({"input_tokens": 3, "output_tokens": 2, "total_tokens": -1}, (3, 0, 2)),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_invalid_counters_do_not_inflate_observed_error_usage(
    bundle, raw, expected, streaming
):
    bundle.responses.raw["usage"] = raw
    p = provider(bundle)
    with pytest.raises(ProtocolError) as caught:
        if streaming:
            [r async for r in p.text_chat_stream(prompt="test")]
        else:
            await p.text_chat(prompt="test")
    usage = caught.value._astrbot_token_usage
    assert (usage.input_other, usage.input_cached, usage.output) == expected
    assert usage.total == sum(expected)
    assert usage.is_partial is True
