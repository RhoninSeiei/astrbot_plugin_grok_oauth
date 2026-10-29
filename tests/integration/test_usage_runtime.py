import asyncio

import httpx
from astrbot_plugin_grok_oauth.astrbot_adapter.provider import GrokOAuthProvider
from astrbot_plugin_grok_oauth.astrbot_adapter.registration import (
    register_provider,
    unregister_provider,
)
from astrbot_plugin_grok_oauth.astrbot_adapter.registry_state import bind_runtime, clear_runtime
from astrbot_plugin_grok_oauth.astrbot_adapter.runtime import GrokRuntime
from astrbot_plugin_grok_oauth.grok_oauth.models import TokenSnapshot


async def test_two_models_share_billing_and_disconnect_clears_account(tmp_path):
    calls = []

    def wire(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "config": {
                    "creditUsagePercent": 12,
                    "productUsage": [
                        {"product": "GrokBuild", "usagePercent": 8},
                        {"product": "GrokChat", "usagePercent": 4},
                        {"product": "GrokImagine", "usagePercent": 0},
                    ],
                }
            },
        )

    runtime = GrokRuntime({}, tmp_path, transport=httpx.MockTransport(wire))
    from astrbot_plugin_grok_oauth.grok_oauth.version import USER_AGENT

    assert runtime.client.headers["user-agent"] == USER_AGENT
    await runtime.open()
    bind_runtime(runtime)
    registration = register_provider(runtime.owner_id, GrokOAuthProvider)
    try:
        await runtime.oauth.bind(
            TokenSnapshot(
                "default",
                "access",
                "refresh",
                None,
                "",
                runtime.client_id,
                user_id="synthetic-user",
            ),
            expected_epoch=0,
        )
        models = [
            GrokOAuthProvider(
                {"id": name, "model": "grok-4.6", "type": "grok_oauth_chat_completion"}, {}
            )
            for name in ["one", "two"]
        ]
        assert all(hasattr(model, "get_usage") for model in models)
        results = await asyncio.gather(
            *(model.get_usage() for model in models), models[0].get_usage_breakdown()
        )
        assert all(result["used_percent"] == 12 for result in results[:2])
        assert results[2]["products"][0]["usage_percent"] == 8
        assert results[2]["status"] == "success"
        assert "products" not in results[0]
        assert calls == ["/v1/billing"]
        await runtime.disconnect("admin")
        result = await models[0].get_usage()
        assert result["status"] == "unbound" and result["used_percent"] is None
        breakdown = await models[1].get_usage_breakdown()
        assert breakdown["status"] == "unbound" and breakdown["products"] == []
        assert calls == ["/v1/billing"]
    finally:
        await runtime.close()
        unregister_provider(registration)
        clear_runtime(runtime)
