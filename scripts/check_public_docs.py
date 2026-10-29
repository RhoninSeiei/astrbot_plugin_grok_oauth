"""Check local links and internal-environment references in public documentation."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from public_files import PUBLIC_DOCS

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    checked = 0
    for name in PUBLIC_DOCS:
        file = root / name
        assert file.is_file(), f"Missing public file: {name}"
        if file.suffix != ".md":
            continue
        text = file.read_text()
        assert not re.search(r"/data/services/|192\.168\.|astrbot-dev[-_]|prod\d+r\d+", text), name
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            path = (file.parent / unquote(parsed.path)).resolve()
            assert path.is_relative_to(root.resolve()), f"Link outside root in {name}"
            assert path.is_file(), f"Broken link in {name}: {target}"
            checked += 1
    print(f"Public documentation checks passed: {checked} relative links")


if __name__ == "__main__":
    check()
