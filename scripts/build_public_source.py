"""Export a reviewable source ZIP without private Git history or operational evidence."""

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from build_package import NAME, ROOT, build

SOURCE_FILES = (
    ".gitignore",
    ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
    ".github/pull_request_template.md",
    "scripts/run_tests.py",
    "scripts/check_architecture.py",
    "scripts/check_sources.py",
    "scripts/verify_integration_report.py",
    "scripts/verify_standard_install.py",
    "scripts/public_files.py",
    "scripts/build_package.py",
    "scripts/build_public_source.py",
    "scripts/check_public_docs.py",
)


def export(output):
    with tempfile.TemporaryDirectory(prefix="grok-public-export-") as temporary:
        manifest = build(Path(temporary) / "runtime.zip")
    files = [ROOT / name for name in (*manifest, *SOURCE_FILES)]
    files.extend(sorted((ROOT / "tests").rglob("*.py")))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
                raise ValueError("Source input outside repository")
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            hashes[relative] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(NAME + "/" + relative, date_time=(2026, 9, 21, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        info = zipfile.ZipInfo(NAME + "/SOURCE-MANIFEST.json", date_time=(2026, 9, 21, 0, 0, 0))
        info.external_attr = 0o100644 << 16
        archive.writestr(info, json.dumps(hashes, indent=2) + "\n")
    if output.stat().st_size >= 16 * 1024 * 1024:
        raise ValueError("Public source ZIP exceeds the market size limit")
    print(
        json.dumps(
            {
                "path": str(output),
                "files": len(hashes),
                "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        )
    )
    return hashes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", default=str(ROOT / "dist/grok-oauth-public-source-candidate.zip")
    )
    export(parser.parse_args().output)
