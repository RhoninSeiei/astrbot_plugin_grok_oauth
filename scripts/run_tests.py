"""Run selected tests against explicit AstrBot source with disposable test state."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))
ASTRBOT_SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", "/AstrBot")).resolve()
sys.path.insert(0, str(ASTRBOT_SOURCE))
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="grok-oauth-test-") as runtime:
        os.environ["ASTRBOT_ROOT"] = runtime
        import pytest

        raise SystemExit(
            pytest.main(
                sys.argv[1:] or [str(ROOT / "tests/unit"), str(ROOT / "tests/integration"), "-q"]
            )
        )
