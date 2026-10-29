"""All private AstrBot integration points for the tested production contract."""

import copy
import inspect
import json

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply, Video
from astrbot.api.provider import LLMResponse, ProviderType
from astrbot.api.provider import Provider as ProviderBase
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response, request
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.config.default import CONFIG_METADATA_2
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider import func_tool_manager as _tool_manager
from astrbot.core.provider.entities import TokenUsage
from astrbot.core.provider.register import (
    provider_cls_map,
    provider_registry,
    register_provider_adapter,
)
from astrbot.core.provider.sources import request_retry
from astrbot.core.provider.sources.openai_responses_source import ProviderOpenAIResponses
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

try:
    from astrbot.core.provider.usage_recorder import (
        observe_usage_snapshot as _observe_usage_snapshot,
    )
except ImportError:
    _observe_usage_snapshot = None

from ..grok_oauth.continuation import make_continuation
from ..grok_oauth.errors import EmptyOutput, ProtocolError, RegistrationConflict
from ..grok_oauth.search import completed_search_calls, search_response_text
from ..grok_oauth.token_usage import safe_token_usage

# Re-exporting keeps private version-dependent imports centralized.
__all__ = [
    "TextPart",
    "AstrBotConfig",
    "AstrMessageEvent",
    "filter",
    "Image",
    "Video",
    "Plain",
    "Reply",
    "Context",
    "Star",
    "register",
    "json_response",
    "request",
    "FunctionTool",
    "ToolSet",
    "request_tool_set",
    "bind_request_tool",
    "MessageChain",
    "LLMResponse",
    "ProviderType",
    "TokenUsage",
    "ProviderBase",
    "provider_cls_map",
    "provider_registry",
    "register_provider_adapter",
    "ProviderOpenAIResponses",
    "get_astrbot_data_path",
    "get_astrbot_plugin_data_path",
    "get_astrbot_temp_path",
    "to_llm_response",
]


def request_tool_set(container):
    """Snapshot host managers through their permission-preserving public API.

    Existing request sets, including UsageToolSet, retain their request bindings.
    Only known host containers are accepted: an unknown iterable is not an empty
    tool selection and must not silently disable tools.
    """
    if container is None:
        return None
    if isinstance(container, _tool_manager.FunctionToolManager):
        snapshot = container.get_full_tool_set()
        if not isinstance(snapshot, ToolSet):
            raise TypeError("AstrBot tool container returned an invalid tool set")
        return snapshot
    if isinstance(container, ToolSet):
        return container
    raise TypeError("Unsupported AstrBot tool container")


def bind_request_tool(tool, handler):
    """Bind a request handler without replacing the host permission guard.

    Current AstrBot guards dispatch through call(), with handler=None. Rebinding
    the guard's handler would bypass its permission check, so only its copied
    inner tool receives the handler. Older cores expose ordinary FunctionTools.
    """
    bound = copy.copy(tool)
    guard_type = getattr(_tool_manager, "_PermissionGuardedTool", None)
    if guard_type is not None and isinstance(tool, guard_type):
        bound._wrapped = bind_request_tool(tool._wrapped, handler)
    else:
        bound.handler = handler
    return bound


def reported_token_usage(raw):
    """Map explicit counters; numeric placeholders never imply reported zeros."""
    safe = safe_token_usage(raw)
    if not safe:
        return None, None
    has_input = "input_tokens" in safe
    has_output = "output_tokens" in safe
    if not (has_input or has_output):
        return None, {"usage": safe, "usage_status": "missing"}
    partial = (
        not (has_input and has_output)
        or safe.get("is_partial") is True
        or (isinstance(raw, dict) and raw.get("is_partial") is True)
    )
    incoming = safe.get("input_tokens", 0)
    # A cache count without its input total cannot be added as more input.
    cached = safe.get("input_tokens_details", {}).get("cached_tokens", 0) if has_input else 0
    usage = TokenUsage(
        input_other=incoming - cached,
        input_cached=cached,
        output=safe.get("output_tokens", 0),
    )
    # Older supported hosts have no constructor argument for this field.
    # Keep the explicit field presence in safe raw metadata for all hosts.
    usage.is_partial = partial
    return usage, {"usage": safe, "usage_status": "partial" if partial else "reported"}


def publish_usage_snapshot(raw, *, response_id=None, complete=False):
    """Bridge to optional request-local host accounting without writing a ledger."""
    if _observe_usage_snapshot is None:
        return
    usage, metadata = reported_token_usage(raw)
    if usage is not None:
        reported_fields = tuple(
            direction
            for direction, key in (("input", "input_tokens"), ("output", "output_tokens"))
            if key in metadata["usage"]
        )
        _observe_usage_snapshot(
            usage,
            response_id=response_id,
            complete=complete,
            reported_fields=reported_fields,
        )


def error_token_usage(error):
    """Recover a snapshot across explicit timeout exception translations only."""
    seen = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        raw = getattr(error, "token_usage", None)
        if isinstance(raw, dict) and raw:
            return raw
        error = getattr(error, "__cause__", None)
    return None


def attach_error_token_usage(error, raw):
    """Expose only observed counters through the host's exception contract."""
    usage, _ = reported_token_usage(raw)
    if usage is not None:
        error._astrbot_token_usage = usage


def to_llm_response(result, *, allow_web_search=False):
    text = result.text
    if result.native_items:
        if not allow_web_search:
            raise ProtocolError("Native server tools were not enabled for this request")
        completed_search_calls(result)
        if not text and not result.function_calls:
            raise EmptyOutput("Search completed without a usable answer")
        text = search_response_text(result)
    reasoning = []
    for item in result.reasoning_items:
        for part in (item.get("summary") or []) + (item.get("content") or []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                reasoning.append(part["text"])
    usage, metadata = reported_token_usage(result.usage)
    return LLMResponse(
        role="tool" if result.function_calls else "assistant",
        result_chain=MessageChain().message(text) if text else None,
        tools_call_args=[json.loads(call["arguments"]) for call in result.function_calls],
        tools_call_name=[call["name"] for call in result.function_calls],
        tools_call_ids=[call["call_id"] for call in result.function_calls],
        reasoning_content="\n".join(reasoning) or None,
        reasoning_signature=make_continuation(result, text),
        id=result.id,
        usage=usage,
        raw_completion=metadata,
    )


async def detach_owned_providers(context, runtime):
    manager = context.provider_manager
    async with manager.reload_lock:
        owned = {
            key: value
            for key, value in manager.inst_map.items()
            if getattr(value, "_runtime", None) is runtime
        }
        preferred = next(
            (key for key, value in owned.items() if value is manager.curr_provider_inst), None
        )
        if owned:
            context._grok_oauth_restore = {"ids": list(owned), "preferred": preferred}
        for provider_id in owned:
            await manager.terminate_provider(provider_id)


async def restore_owned_providers(context, runtime):
    pending = getattr(context, "_grok_oauth_restore", None)
    if not pending:
        return  # Cold startup: the core initializes providers after plugins.
    manager = context.provider_manager
    async with manager.reload_lock:
        previous = manager.curr_provider_inst
        for config in manager.providers_config:
            if config.get("id") not in pending["ids"] or not config.get("enable"):
                continue
            existing = manager.inst_map.get(config["id"])
            if existing is not None:
                if getattr(existing, "_runtime", None) is not runtime:
                    raise RegistrationConflict("A provider ID was reused during plugin reload")
                continue
            merge = manager.get_merged_provider_config
            parameters = inspect.signature(merge).parameters
            supports_runtime = "runtime" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            )
            options = {"runtime": True} if supports_runtime else {}
            merged = merge(copy.deepcopy(config), **options)
            if merged.get("type") != "grok_oauth_chat_completion":
                continue
            await manager.load_provider(copy.deepcopy(config))
            if getattr(manager.inst_map.get(config["id"]), "_runtime", None) is not runtime:
                raise ProtocolError("Target provider did not restore after reload")
        if previous is not None:
            manager.curr_provider_inst = previous
        elif pending["preferred"] in manager.inst_map:
            manager.curr_provider_inst = manager.inst_map[pending["preferred"]]
        delattr(context, "_grok_oauth_restore")


def install_owned_tools(context, tools):
    manager = context.get_llm_tool_manager()
    names = {tool.name for tool in tools}
    if any(tool.name in names for tool in manager.func_list):
        raise RegistrationConflict("Grok tool names belong to another registration")
    context.add_llm_tools(*tools)


def remove_owned_tools(context, tools):
    owned = {id(tool) for tool in tools}
    manager = context.get_llm_tool_manager()
    manager.func_list[:] = [tool for tool in manager.func_list if id(tool) not in owned]


def provider_source_templates():
    return CONFIG_METADATA_2["provider_group"]["metadata"]["provider"]["config_template"]


async def migrate_legacy_provider_sources(context):
    from ..grok_oauth.catalog import ModelCatalog
    from .source_compat import plan_legacy_source_links, plan_native_model_configs

    manager = context.provider_manager
    async with manager.resource_lock:
        config = manager.acm.default_conf
        original_models = config.get("provider", [])
        original_sources = config.get("provider_sources", [])
        planned = plan_legacy_source_links(original_models, original_sources)
        models, sources = planned[:2] if planned else (original_models, original_sources)
        catalog = ModelCatalog()
        defaults = {}
        for model in models:
            name = model.get("model") or "grok-4.6"
            capabilities = catalog.capabilities(name)
            defaults[name] = {
                "modalities": ["text"]
                + [
                    modality
                    for capability, modality in (
                        ("vision", "image"),
                        ("function_tools", "tool_use"),
                    )
                    if capabilities[capability]["model_support"] == "available"
                ]
            }
        normalized = plan_native_model_configs(models, sources, model_defaults=defaults)
        if normalized is not None:
            models = normalized
        if models == original_models and sources == original_sources:
            return 0
        count = sum(before != after for before, after in zip(original_models, models))
        try:
            # The native save commits atomically. Keep this short transaction
            # synchronous under the model configuration lock, without starting
            # providers before the host's cold-start initialization phase.
            config.save_config({"provider": models, "provider_sources": sources})
        except BaseException:
            config["provider"] = original_models
            config["provider_sources"] = original_sources
            raise
        manager.providers_config = config["provider"]
        manager.provider_sources_config = config["provider_sources"]
        return count


def inherited_oauth_web_search():
    """Read the request-scoped host policy without changing shared state."""
    policy = getattr(request_retry, "provider_oauth_web_search", None)
    # Stock AstrBot has no OAuth search ContextVar. Keep the provider default;
    # explicit request controls and plugin enable flags still apply.
    return policy.get() if policy is not None else "inherit"


async def selected_chat_provider(context, event):
    # The normal host Agent can change its provider after a primary failure.
    # Match the event by identity, never borrow another request in the same UMO.
    from astrbot.core.pipeline.process_stage import follow_up

    runners = getattr(follow_up, "_ACTIVE_AGENT_RUNNERS", {})
    runner = runners.get(event.unified_msg_origin)
    runner_event = getattr(
        getattr(getattr(runner, "run_context", None), "context", None), "event", None
    )
    if runner is not None and runner_event is event:
        return runner.provider
    selected = event.get_extra("selected_provider")
    if isinstance(selected, str) and selected:
        return context.get_provider_by_id(selected)
    try:
        return await context.get_using_provider_async(event.unified_msg_origin)
    except ValueError:
        return None
