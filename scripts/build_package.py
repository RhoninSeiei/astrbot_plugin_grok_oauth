"""Build a deterministic allowlisted plugin archive and SHA-256 manifest."""

import argparse
import hashlib
import json
import re
import runpy
import tomllib
import zipfile
from pathlib import Path

from public_files import PUBLIC_DOCS

ROOT = Path(__file__).resolve().parents[1]
NAME = "astrbot_plugin_grok_oauth"
VERSION = runpy.run_path(str(ROOT / "grok_oauth" / "version.py"))["__version__"]


def build(output):
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    metadata_version = re.search(r"^version: v(.*)$", (ROOT / "metadata.yaml").read_text(), re.M)
    if metadata_version is None or project_version != VERSION or metadata_version[1] != VERSION:
        raise ValueError("Release versions do not match")
    files = [
        ROOT / name
        for name in (
            "__init__.py",
            "main.py",
            "metadata.yaml",
            "_conf_schema.json",
            "requirements.txt",
            "constraints-production.txt",
            "pyproject.toml",
        )
    ]
    files.extend(ROOT / name for name in PUBLIC_DOCS)
    for directory in ("grok_oauth", "astrbot_adapter"):
        files.extend(sorted((ROOT / directory).glob("*.py")))
    for directory, suffixes in (("pages", {".html", ".js", ".css"}), ("i18n", {".json"})):
        files.extend(
            sorted(path for path in (ROOT / directory).rglob("*") if path.suffix in suffixes)
        )
    manifest = {}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            if file.is_symlink() or not file.resolve().is_relative_to(ROOT.resolve()):
                raise ValueError("Package input outside source root")
            relative = file.relative_to(ROOT).as_posix()
            data = file.read_bytes()
            manifest[relative] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(NAME + "/" + relative, date_time=(2026, 9, 7, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        info = zipfile.ZipInfo(NAME + "/MANIFEST.sha256.json", date_time=(2026, 9, 7, 0, 0, 0))
        info.external_attr = 0o100644 << 16
        archive.writestr(info, json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(json.dumps({"path": str(output.resolve()), "sha256": digest, "files": len(manifest)}))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "dist" / (NAME + "-" + VERSION + ".zip")))
    build(parser.parse_args().output)
