import copy

import pytest

from astrbot_adapter import source_compat

TYPE = source_compat.PROVIDER_TYPE
SOURCE = {
    "id": "grok",
    "type": TYPE,
    "provider_type": "chat_completion",
    "api_base": "https://api.x.ai/v1",
    "timeout": 180,
    "key": ["oauth-managed"],
    "grok_account_slot": "default",
}
DEFAULTS = {"grok-4.7": {"modalities": ["text", "image", "tool_use"]}}


def normalize(models, sources):
    assert hasattr(source_compat, "plan_native_model_configs")
    return source_compat.plan_native_model_configs(models, sources, model_defaults=DEFAULTS)


def test_valid_linked_legacy_model_becomes_native_child_without_input_mutation():
    model = {
        **SOURCE,
        "id": "stable-4.6",
        "provider_source_id": "grok",
        "model": "grok-4.7",
        "enable": True,
    }
    before = copy.deepcopy(model)
    result = normalize([model], [SOURCE])
    assert result == [
        {
            "id": "stable-4.6",
            "provider_source_id": "grok",
            "model": "grok-4.7",
            "enable": True,
            "modalities": ["text", "image", "tool_use"],
            "max_context_tokens": 0,
            "custom_extra_body": {},
        }
    ]
    assert model == before
    assert normalize(result, [SOURCE]) is None


def test_existing_native_model_settings_are_preserved():
    model = {
        "id": "native",
        "provider_source_id": "grok",
        "model": "grok-4.7",
        "enable": False,
        "modalities": [],
        "max_context_tokens": 12345,
        "custom_extra_body": {"temperature": 0.2},
    }
    assert normalize([model], [SOURCE]) is None


def test_source_inherited_model_values_are_not_shadowed_by_defaults():
    source = {
        **SOURCE,
        "modalities": ["text"],
        "max_context_tokens": 12345,
        "custom_extra_body": {"temperature": 0.3},
    }
    model = {"id": "model", "model": "grok-4.7", "provider_source_id": "grok"}
    result = normalize([model], [source])[0]
    for key in ["modalities", "max_context_tokens", "custom_extra_body"]:
        assert result[key] == source[key]
    assert result["custom_extra_body"] is not source["custom_extra_body"]


def test_different_connection_overrides_and_unknown_fields_survive():
    model = {
        "id": "model",
        "model": "grok-4.7",
        "provider_source_id": "grok",
        "type": TYPE,
        "timeout": 45,
        "api_base": "https://other.invalid",
        "custom_setting": "keep",
    }
    result = normalize([model], [SOURCE])[0]
    assert (
        result["timeout"] == 45
        and result["api_base"] == model["api_base"]
        and result["custom_setting"] == "keep"
    )
    assert "type" not in result


@pytest.mark.parametrize("sources", [[], [{**SOURCE, "type": "other"}], [SOURCE, SOURCE]])
def test_missing_other_or_ambiguous_source_is_not_touched(sources):
    assert normalize([{"id": "model", "provider_source_id": "grok", "type": TYPE}], sources) is None


def test_unsupported_account_slot_and_other_models_are_untouched():
    models = [
        {"id": "other-slot", "provider_source_id": "grok", "grok_account_slot": "other"},
        {"id": "other-provider", "provider_source_id": "elsewhere"},
    ]
    assert normalize(models, [SOURCE]) is None


def test_unknown_model_has_conservative_native_fields():
    result = normalize(
        [{"id": "unknown", "model": "future", "provider_source_id": "grok"}], [SOURCE]
    )[0]
    assert result["modalities"] == ["text"] and result["max_context_tokens"] == 0


def test_known_placeholder_can_be_removed_without_source_key():
    source = {k: v for k, v in SOURCE.items() if k != "key"}
    result = normalize(
        [
            {
                "id": "old",
                "model": "grok-4.7",
                "provider_source_id": "grok",
                "key": ["oauth-managed"],
            }
        ],
        [source],
    )[0]
    assert "key" not in result


def test_default_account_slot_does_not_remain_on_native_model_without_source_slot():
    source = {k: v for k, v in SOURCE.items() if k != "grok_account_slot"}
    model = {
        "id": "old",
        "model": "grok-4.7",
        "provider_source_id": "grok",
        "grok_account_slot": "default",
    }
    result = normalize([model], [source])[0]
    assert "grok_account_slot" not in result
