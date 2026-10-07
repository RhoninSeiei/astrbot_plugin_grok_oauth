import json
import subprocess
import sys
from pathlib import Path


def test_standard_plugin_installer_and_named_reload(tmp_path):
    root = Path(__file__).resolve().parents[2]
    archive = tmp_path / "plugin.zip"
    build = subprocess.run(
        [sys.executable, "-B", str(root / "scripts/build_package.py"), "--output", str(archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [sys.executable, "-B", str(root / "scripts/verify_standard_install.py"), str(archive)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summaries = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.startswith("{") and "standard_zip_install" in line
    ]
    assert len(summaries) == 1 and summaries[0]["standard_zip_install"] == "passed"
    assert (
        summaries[0]["dependency_installations"] == 0 and summaries[0]["owned_cleanup"] == "passed"
    )
