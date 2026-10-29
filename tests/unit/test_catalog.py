import pytest

from grok_oauth.catalog import ModelCatalog
from grok_oauth.errors import UnsupportedModelParameter


@pytest.mark.parametrize("model", ["grok-4.7", "grok-4.7-build-fast"])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_47_known_models_reasoning_and_capabilities(model, effort):
    catalog = ModelCatalog()
    assert model in catalog.chat_models()
    assert catalog.validate_reasoning(model, effort) == {"effort": effort}
    for capability in ["chat", "streaming", "vision", "function_tools"]:
        assert catalog.capabilities(model)[capability]["model_support"] == "available"
        assert catalog.capabilities(model)[capability]["observed"] == "unknown"


@pytest.mark.parametrize("model", ["grok-4.7", "grok-4.7-build-fast"])
@pytest.mark.parametrize("effort", ["none", "max", True])
def test_47_rejects_unsupported_reasoning(model, effort):
    with pytest.raises(UnsupportedModelParameter):
        ModelCatalog.validate_reasoning(model, effort)


async def test_catalog_refresh_retains_known_build_alias_only_with_base_model():
    class Models:
        async def request_json(self, method, path, *, policy):
            return {
                "data": [{"id": "grok-4.7"}, {"id": "grok-4.6"}, {"id": "grok-imagine-image-2.0"}]
            }

    catalog = ModelCatalog()
    await catalog.refresh(Models())
    assert set(catalog.chat_models()) == {"grok-4.7", "grok-4.7-build-fast", "grok-4.6"}
    assert catalog.capabilities("grok-4.7-build-fast")["chat"]["observed"] == "unknown"


async def test_catalog_refresh_without_base_does_not_offer_build_alias():
    class Models:
        async def request_json(self, method, path, *, policy):
            return {"data": [{"id": "grok-4.6"}]}

    catalog = ModelCatalog()
    await catalog.refresh(Models())
    assert catalog.chat_models() == ["grok-4.6"]
