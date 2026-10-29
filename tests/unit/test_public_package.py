import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def package_builder(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "public_package_test", ROOT / "scripts/build_package.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_package_contains_linked_guide_and_no_internal_records(package_builder, tmp_path):
    output = tmp_path / "plugin.zip"
    manifest = package_builder.build(output)
    assert "docs/USER_GUIDE.md" in manifest
    assert "docs/images/01-authorize.png" in manifest
    assert not any(
        "production" in name or "evidence" in name for name in manifest if name.startswith("docs/")
    )
    with zipfile.ZipFile(output) as archive:
        for name in manifest:
            assert archive.read(package_builder.NAME + "/" + name) == (ROOT / name).read_bytes()


def test_public_package_rejects_symlink_document(package_builder, tmp_path, monkeypatch):
    secret = tmp_path / "outside.txt"
    secret.write_text("synthetic private document")
    link = tmp_path / "guide.md"
    link.symlink_to(secret)
    monkeypatch.setattr(package_builder, "PUBLIC_DOCS", (str(link),))
    with pytest.raises(ValueError, match="outside source root"):
        package_builder.build(tmp_path / "plugin.zip")
