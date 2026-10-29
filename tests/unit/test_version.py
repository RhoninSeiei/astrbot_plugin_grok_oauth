import importlib
import re
import tomllib
from pathlib import Path


def test_all_transport_versions_match_release_metadata():
    package = importlib.import_module("grok_oauth")
    oauth = importlib.import_module("grok_oauth.oauth")
    http = importlib.import_module("grok_oauth.http")
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    metadata = re.search(r"^version: v(.*)$", (root / "metadata.yaml").read_text(), re.M).group(1)
    assert package.__version__ == project["version"] == metadata
    assert oauth.USER_AGENT == http.USER_AGENT == "astrbot-plugin-grok-oauth/" + metadata
