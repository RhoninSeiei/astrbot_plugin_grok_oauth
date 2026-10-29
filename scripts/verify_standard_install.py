"""Exercise the deployed ZIP installer and exact named reload in disposable state."""

import argparse
import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


async def verify(archive, root, *, allow_dependency_install=False, repo_url=None):
    from astrbot.api.star import Context
    from astrbot.core import astrbot_config, db_helper, pip_installer, sp
    from astrbot.core.provider.manager import ProviderManager
    from astrbot.core.provider.register import provider_cls_map
    from astrbot.core.star.star import star_registry
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.utils.metrics import Metric

    dependency_installations = 0
    original_install = pip_installer.install

    async def tracked_install(**kwargs):
        nonlocal dependency_installations
        if not allow_dependency_install:
            raise AssertionError("Installer must reuse satisfied dependencies")
        dependency_installations += 1
        return await original_install(**kwargs)

    async def no_telemetry(**kwargs):
        return None

    pip_installer.install = tracked_install
    Metric.upload = no_telemetry
    await db_helper.initialize()
    await sp.initialize()
    astrbot_config["provider"] = []
    astrbot_config["provider_sources"] = []
    acm = SimpleNamespace(confs={"default": astrbot_config}, default_conf=astrbot_config)
    persona = SimpleNamespace(default_persona="default")
    pm = ProviderManager(acm, db_helper, persona)
    context = Context(
        event_queue=asyncio.Queue(),
        config=astrbot_config,
        db=db_helper,
        provider_manager=pm,
        platform_manager=SimpleNamespace(platform_insts=[]),
        conversation_manager=None,
        message_history_manager=None,
        persona_manager=persona,
        astrbot_config_mgr=acm,
        knowledge_base_manager=None,
        cron_manager=None,
    )
    context.registered_web_apis = []
    manager = PluginManager(context, astrbot_config)
    await asyncio.to_thread(Path(manager.plugin_store_path).mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(Path(manager.plugin_config_path).mkdir, parents=True, exist_ok=True)
    name = "astrbot_plugin_grok_oauth"
    meta = None
    try:
        if repo_url:
            info = await manager.install_plugin(repo_url)
        else:
            copy = Path(root) / "upload.zip"
            shutil.copyfile(archive, copy)
            info = await manager.install_plugin_from_file(str(copy))
        assert info["name"] == name
        matches = [s for s in star_registry if s.name == name]
        assert len(matches) == 1 and matches[0].activated
        meta = matches[0]
        assert meta.module_path == "data.plugins." + name + ".main"
        assert "grok_oauth_chat_completion" in provider_cls_map
        assert len(context.registered_web_apis) == 18
        assert {route[0] for route in context.registered_web_apis} >= {
            "/grok-oauth/auth/status",
            "/astrbot_plugin_grok_oauth/auth/status",
        }
        assert (Path(manager.plugin_store_path) / name / "pages/oauth/index.html").is_file()
        assert (
            len(
                [
                    tool
                    for tool in pm.llm_tools.func_list
                    if tool.name
                    in {
                        "grok_image_generate",
                        "grok_image_edit",
                        "grok_web_search",
                        "grok_usage_status",
                        "grok_usage_breakdown",
                        "grok_video_generate",
                        "grok_video_edit",
                        "grok_video_status",
                    }
                ]
            )
            == 8
        )
        assert all(
            tool.handler_module_path == meta.module_path
            for tool in pm.llm_tools.func_list
            if tool.name
            in {
                "grok_image_generate",
                "grok_image_edit",
                "grok_web_search",
                "grok_usage_status",
                "grok_usage_breakdown",
                "grok_video_generate",
                "grok_video_edit",
                "grok_video_status",
            }
        )
        models = importlib.import_module("data.plugins." + name + ".grok_oauth.models")
        runtime = meta.star_cls.runtime
        await runtime.oauth.bind(
            models.TokenSnapshot(
                "default",
                "integration-only-access",
                "integration-only-refresh",
                None,
                "api:access",
                runtime.client_id,
            ),
            expected_epoch=runtime.oauth.epoch,
        )
        for index in (1, 2):
            config = {
                "id": "grok-" + str(index),
                "type": "grok_oauth_chat_completion",
                "provider_type": "chat_completion",
                "model": "grok-4.6",
                "enable": True,
            }
            pm.providers_config.append(config)
            await pm.load_provider(config)
        old_instances = [pm.inst_map["grok-" + str(index)] for index in (1, 2)]
        assert old_instances[0]._runtime is old_instances[1]._runtime
        assert {"grok-4.7", "grok-4.7-build-fast"} <= set(await old_instances[0].get_models())
        unrelated = SimpleNamespace(name="unrelated")
        pm.inst_map["unrelated"] = unrelated
        pm.curr_provider_inst = unrelated
        # Exact known name only; this host falls back to all-plugin reload on unknown names.
        success, error = await manager.reload(name)
        assert success, error
        matches = [s for s in star_registry if s.name == name]
        assert len(matches) == 1
        meta = matches[0]
        assert meta.star_cls.runtime is not runtime and runtime.closed
        assert meta.star_cls.runtime.oauth.snapshot().access_token == "integration-only-access"
        assert pm.inst_map["unrelated"] is unrelated and pm.curr_provider_inst is unrelated
        assert all(
            pm.inst_map["grok-" + str(i + 1)] is not old for i, old in enumerate(old_instances)
        )
        assert (
            len(
                [
                    x
                    for x in pm.provider_insts
                    if x.provider_config["type"] == "grok_oauth_chat_completion"
                ]
            )
            == 2
        )
        await manager._terminate_plugin(meta)
        await manager._unbind_plugin(name, meta.module_path)
        meta = None
        assert "grok_oauth_chat_completion" not in provider_cls_map
        assert pm.inst_map == {"unrelated": unrelated}
        assert not [
            tool
            for tool in pm.llm_tools.func_list
            if tool.name
            in {
                "grok_image_generate",
                "grok_image_edit",
                "grok_web_search",
                "grok_usage_status",
                "grok_usage_breakdown",
                "grok_video_generate",
                "grok_video_edit",
                "grok_video_status",
            }
        ]
        print(
            json.dumps(
                {
                    "installation": "repository" if repo_url else "zip",
                    "standard_zip_install": "not_run" if repo_url else "passed",
                    "pages_and_control_routes": "passed",
                    "named_reload": "passed",
                    "shared_models": 2,
                    "credentials_restored": "synthetic_only",
                    "unrelated_provider_unchanged": True,
                    "owned_cleanup": "passed",
                    "dependency_installations": dependency_installations,
                    "real_account_calls": 0,
                }
            )
        )
    finally:
        if meta and getattr(meta, "star_cls", None):
            await meta.star_cls.terminate()
        await sp.close()
        await db_helper.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", nargs="?")
    parser.add_argument("--repo-url")
    parser.add_argument(
        "--allow-dependency-install",
        action="store_true",
        help="Only use in a disposable environment; permits real host installer",
    )
    args = parser.parse_args()
    if bool(args.archive) == bool(args.repo_url):
        parser.error("Specify exactly one archive or --repo-url")
    archive = Path(args.archive).resolve() if args.archive else None
    with tempfile.TemporaryDirectory(prefix="grok-standard-install-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        os.environ["ASTRBOT_RELOAD"] = "0"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.chdir(root)
        sys.path.insert(0, root)
        sys.path.insert(0, str(Path(os.environ.get("ASTRBOT_SOURCE", "/AstrBot")).resolve()))
        asyncio.run(
            verify(
                archive,
                root,
                allow_dependency_install=args.allow_dependency_install,
                repo_url=args.repo_url,
            )
        )
