import copy

from astrbot_adapter.source_compat import PROVIDER_TYPE, plan_legacy_source_links


def test_existing_links_and_other_types_need_no_migration():
    models = [
        {"id": "kept", "type": PROVIDER_TYPE, "provider_source_id": "chosen"},
        {"id": "other", "type": "unrelated"},
    ]
    assert plan_legacy_source_links(models, [{"id": "chosen", "type": PROVIDER_TYPE}]) is None


def test_dangling_link_is_repaired_without_changing_model_identity_or_parameters():
    models = [
        {
            "id": "custom",
            "type": PROVIDER_TYPE,
            "provider_source_id": "gone",
            "model": "grok-4.3",
            "enable": False,
        }
    ]
    sources = [{"id": "present", "type": PROVIDER_TYPE, "enable": True}]
    before = copy.deepcopy((models, sources))
    result_models, result_sources, count = plan_legacy_source_links(models, sources)
    assert result_models == [{**models[0], "provider_source_id": "present"}]
    assert result_sources == sources and count == 1
    assert (models, sources) == before


def test_multiple_sources_use_separate_source_and_do_not_guess_by_name():
    sources = [
        {"id": name, "type": PROVIDER_TYPE} for name in ("left", "right", "grok_oauth_legacy")
    ]
    models, updated, count = plan_legacy_source_links(
        [{"id": "left/anything", "type": PROVIDER_TYPE}], sources
    )
    assert models[0]["provider_source_id"] == "grok_oauth_legacy_2"
    assert updated[:-1] == sources and updated[-1]["id"] == "grok_oauth_legacy_2"
    assert count == 1


def test_unsupported_account_slot_is_not_reassigned():
    assert (
        plan_legacy_source_links(
            [{"id": "custom", "type": PROVIDER_TYPE, "grok_account_slot": "other"}], []
        )
        is None
    )


def test_empty_source_id_does_not_hide_a_missing_link():
    models, sources, count = plan_legacy_source_links(
        [{"id": "old", "type": PROVIDER_TYPE}], [{"id": None, "type": "other"}]
    )
    assert count == 1 and models[0]["provider_source_id"] == "grok_oauth"
    assert sources[0] == {"id": None, "type": "other"}


def test_enabled_legacy_model_is_not_linked_to_disabled_source():
    model = {"id": "existing", "type": PROVIDER_TYPE, "enable": True}
    source = {"id": "disabled", "type": PROVIDER_TYPE, "enable": False}
    models, sources, _ = plan_legacy_source_links([model], [source])
    assert models[0]["provider_source_id"] != "disabled"
    assert sources[0] == source and sources[1]["enable"] is True


def test_legacy_template_matching_distinguishes_json_boolean_from_integer():
    from astrbot_adapter.source_compat import is_legacy_source_template

    old = {
        "id": PROVIDER_TYPE,
        "type": PROVIDER_TYPE,
        "provider_type": "chat_completion",
        "enable": False,
        "key": ["oauth-managed"],
        "api_base": "https://api.x.ai/v1",
        "model": "grok-4.6",
        "grok_account_slot": "default",
        "timeout": 180,
    }
    assert is_legacy_source_template(old)
    assert not is_legacy_source_template({**old, "enable": 0})
    assert not is_legacy_source_template({**old, "timeout": 180.0})
