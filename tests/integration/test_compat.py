"""Optional host extensions preserve request ownership and restrictive policies."""

import importlib.util
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.pipeline.process_stage import follow_up
from astrbot.core.provider.sources import request_retry
from astrbot_plugin_grok_oauth.astrbot_adapter import compat


def test_compat_import_without_custom_search_extension(monkeypatch):
    monkeypatch.delattr(request_retry, "provider_oauth_web_search", raising=False)
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_grok_oauth.astrbot_adapter._compat_stock_test", compat.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.inherited_oauth_web_search() == "inherit"


@pytest.mark.parametrize("policy", ["inherit", "live", "disabled", "cached"])
def test_optional_search_context_is_read_without_mutation(monkeypatch, policy):
    inherited = ContextVar("grok_test_search_inheritance", default="inherit")
    monkeypatch.setattr(request_retry, "provider_oauth_web_search", inherited, raising=False)
    token = inherited.set(policy)
    try:
        assert compat.inherited_oauth_web_search() == policy
        assert inherited.get() == policy
    finally:
        inherited.reset(token)


@pytest.mark.parametrize("matching_event", [False, True])
async def test_runner_provider_requires_exact_event_identity(monkeypatch, matching_event):
    event = SimpleNamespace(
        unified_msg_origin="test:GroupMessage:1", get_extra=lambda key: "chosen"
    )
    active_event = (
        event if matching_event else SimpleNamespace(unified_msg_origin=event.unified_msg_origin)
    )
    active, chosen = object(), object()
    runner = SimpleNamespace(
        provider=active, run_context=SimpleNamespace(context=SimpleNamespace(event=active_event))
    )
    monkeypatch.setattr(follow_up, "_ACTIVE_AGENT_RUNNERS", {event.unified_msg_origin: runner})
    context = SimpleNamespace(
        get_provider_by_id=lambda identifier: chosen if identifier == "chosen" else None,
        get_using_provider_async=AsyncMock(side_effect=AssertionError("must honor selection")),
    )
    assert await compat.selected_chat_provider(context, event) is (
        active if matching_event else chosen
    )
    context.get_using_provider_async.assert_not_called()


@pytest.mark.parametrize("selected", [None, "chosen", "unknown"])
async def test_missing_runner_registry_uses_event_then_host_selection(monkeypatch, selected):
    monkeypatch.delattr(follow_up, "_ACTIVE_AGENT_RUNNERS", raising=False)
    chosen, default = object(), object()
    event = SimpleNamespace(
        unified_msg_origin="test:GroupMessage:1", get_extra=lambda key: selected
    )
    current = AsyncMock(return_value=default)
    context = SimpleNamespace(
        get_provider_by_id=lambda identifier: chosen if identifier == "chosen" else None,
        get_using_provider_async=current,
    )
    result = await compat.selected_chat_provider(context, event)
    assert result is (default if selected is None else chosen if selected == "chosen" else None)
    if selected is None:
        current.assert_awaited_once_with(event.unified_msg_origin)
    else:
        current.assert_not_called()


@pytest.mark.parametrize("has_runtime_option", [False, True])
async def test_restore_uses_actual_host_merge_signature(has_runtime_option):
    import asyncio

    runtime = object()
    restored = SimpleNamespace(_runtime=runtime)
    model = {"id": "owned", "type": "grok_oauth_chat_completion", "enable": True}
    seen = []
    if has_runtime_option:

        def merge(config, *, runtime=False):
            seen.append((config, runtime))
            return config
    else:

        def merge(config):
            seen.append((config, None))
            return config

    manager = SimpleNamespace(
        reload_lock=asyncio.Lock(),
        curr_provider_inst=None,
        providers_config=[model],
        inst_map={},
        get_merged_provider_config=merge,
    )

    async def load(config):
        manager.inst_map[config["id"]] = restored

    manager.load_provider = AsyncMock(side_effect=load)
    context = SimpleNamespace(
        provider_manager=manager, _grok_oauth_restore={"ids": ["owned"], "preferred": "owned"}
    )
    await compat.restore_owned_providers(context, runtime)
    assert seen == [(model, True if has_runtime_option else None)]
    assert seen[0][0] is not model
    manager.load_provider.assert_awaited_once_with(model)
    assert manager.curr_provider_inst is restored
    assert not hasattr(context, "_grok_oauth_restore")


async def test_restore_does_not_swallow_internal_merge_type_error():
    import asyncio

    error = TypeError("internal host failure")

    def merge(config, *, runtime=False):
        raise error

    manager = SimpleNamespace(
        reload_lock=asyncio.Lock(),
        curr_provider_inst=None,
        providers_config=[{"id": "owned", "enable": True}],
        inst_map={},
        get_merged_provider_config=merge,
        load_provider=AsyncMock(),
    )
    context = SimpleNamespace(
        provider_manager=manager, _grok_oauth_restore={"ids": ["owned"], "preferred": "owned"}
    )
    with pytest.raises(TypeError) as caught:
        await compat.restore_owned_providers(context, object())
    assert caught.value is error
    manager.load_provider.assert_not_called()
    assert hasattr(context, "_grok_oauth_restore")
