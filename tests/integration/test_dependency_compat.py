from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("pillow_version", ["12.2.0", "13.0.0"])
async def test_core_installer_reuses_supported_host_pillow(monkeypatch, pillow_version):
    from astrbot.core.star import star_manager
    from astrbot.core.utils import requirements_utils

    # This models the host's installed distribution metadata; the actual core
    # precheck must accept either supported major without invoking pip.
    installed = {"httpx": "0.28.1", "pillow": pillow_version, "filelock": "3.29.1"}
    monkeypatch.setattr(
        requirements_utils, "collect_installed_distribution_versions", lambda _: installed
    )
    installs = []

    async def reject_dependency_replacement(**kwargs):
        installs.append(kwargs)
        raise AssertionError("A supported host Pillow must not trigger dependency replacement")

    monkeypatch.setattr(star_manager.pip_installer, "install", reject_dependency_replacement)
    requirements = ROOT / "requirements.txt"
    await star_manager._install_requirements_with_precheck(
        plugin_label="astrbot_plugin_grok_oauth", requirements_path=str(requirements)
    )
    assert installs == []
