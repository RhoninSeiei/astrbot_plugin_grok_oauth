"""Contract for customized AstrBot rate-limit fallback controls.

Run separately against a host that implements ProviderRequest fallback_on_rate_limit.
The stock integration suite does not claim these host-only controls.
"""

import weakref
from types import SimpleNamespace

import pytest
from astrbot_plugin_grok_oauth.astrbot_adapter.provider import GrokOAuthProvider
from astrbot_plugin_grok_oauth.astrbot_adapter.registration import (
    register_provider,
    unregister_provider,
)
from astrbot_plugin_grok_oauth.astrbot_adapter.registry_state import bind_runtime, clear_runtime
from astrbot_plugin_grok_oauth.grok_oauth.catalog import ModelCatalog
from astrbot_plugin_grok_oauth.grok_oauth.responses import ResponseEvent, normalize_response

TYPE = "grok_oauth_chat_completion"


class Responses:
    def __init__(self):
        self.calls = []
        self.raw = dict(
            id="r1",
            model="grok-4.6",
            status="completed",
            output=[{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        )

    async def create(self, payload, *, policy):
        self.calls.append((payload, policy))
        return normalize_response(self.raw)

    async def stream(self, payload, *, policy):
        self.calls.append((payload, policy))
        yield ResponseEvent("text_delta", delta="o")
        yield ResponseEvent("text_delta", delta="k")
        yield ResponseEvent("completed", result=normalize_response(self.raw))


@pytest.fixture
def bundle():
    runtime = SimpleNamespace(
        closed=False,
        responses=Responses(),
        catalog=ModelCatalog(),
        config={},
        owner_id="test-runtime",
        providers=weakref.WeakSet(),
    )
    bind_runtime(runtime)
    handle = register_provider(runtime.owner_id, GrokOAuthProvider)
    yield runtime
    unregister_provider(handle)
    clear_runtime(runtime)


def provider(bundle, **kwargs):
    return GrokOAuthProvider({"type": TYPE, "id": "grok-test", "model": "grok-4.6", **kwargs}, {})


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("fallback_allowed", [False, True])
async def test_real_agent_respects_rate_limit_fallback_policy(bundle, streaming, fallback_allowed):
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot_plugin_grok_oauth.grok_oauth.errors import RateLimited

    primary = provider(bundle)
    backup = provider(bundle, id="backup")
    attempts = []

    async def fail(**kwargs):
        attempts.append("primary")
        raise RateLimited(request_id="test-limit")

    async def fail_stream(**kwargs):
        await fail(**kwargs)
        yield

    primary.text_chat = fail
    primary.text_chat_stream = fail_stream
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=primary,
        request=ProviderRequest(
            prompt="test",
            retry_rate_limits=False,
            fallback_on_rate_limit=fallback_allowed,
            oauth_web_search="disabled",
        ),
        run_context=ContextWrapper(context=SimpleNamespace(), tool_call_timeout=10),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks(),
        streaming=streaming,
    )
    runner.fallback_providers = [backup]
    results = [item async for item in runner._iter_llm_responses_with_fallback()]
    assert attempts == ["primary"]
    assert len(bundle.responses.calls) == int(fallback_allowed)
    if fallback_allowed:
        assert results[-1].completion_text == "ok"
    else:
        assert results[-1].role == "err" and results[-1].status_code == 429
