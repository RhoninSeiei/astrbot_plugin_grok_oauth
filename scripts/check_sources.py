"""Validate explicit source identities without treating blob SHAs as commits."""

import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
refs = json.loads((root / "docs/SOURCE_REFS.json").read_text())
for repo in refs["repositories"]:
    assert re.fullmatch("[0-9a-f]{40}", repo["commit"])
    assert repo["license_spdx"] and repo["license_path"]
    for file in repo["files"]:
        assert re.fullmatch("[0-9a-f]{40}", file["blob_sha"])
        assert re.fullmatch("[0-9a-f]{64}", file["sha256"])
        assert "/blob/" + repo["commit"] + "/" in file["url"]
for entry in refs["protocol_sources"]:
    assert entry["url"].startswith("https://") and entry["used_for"]
for entry in refs["not_adopted"]:
    assert entry["repository"] and entry["reason"]
print("Source identity and explicit adoption checks passed")
