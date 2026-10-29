import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_protocol_imports_without_any_astrbot_module():
    code = """
import importlib,sys
from pathlib import Path
class BlockHost:
    def find_spec(self, fullname, path=None, target=None):
        if fullname=='astrbot' or fullname.startswith('astrbot.'):
            raise AssertionError('Protocol imported host')
sys.meta_path.insert(0,BlockHost())
sys.path.insert(0,sys.argv[1])
for path in (Path(sys.argv[1])/'grok_oauth').glob('*.py'):
    importlib.import_module('grok_oauth.'+path.stem)
assert not any(name=='astrbot' or name.startswith('astrbot.') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(ROOT)],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_architecture_and_source_scripts():
    for script in ("check_architecture.py", "check_sources.py"):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / script)],
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_integration_report_rejects_empty_and_skipped_suites(tmp_path):
    for xml, exit_code in [
        ("<testsuite/>", 1),
        ("<testsuite><testcase><skipped/></testcase></testsuite>", 1),
        ("<testsuite><testcase/></testsuite>", 0),
    ]:
        report = tmp_path / "result.xml"
        report.write_text(xml)
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/verify_integration_report.py"), str(report)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == exit_code


def test_runtime_logging_uses_only_host_injected_loggers():
    import ast

    paths = [
        ROOT / "main.py",
        *(ROOT / "grok_oauth").glob("*.py"),
        *(ROOT / "astrbot_adapter").glob("*.py"),
    ]
    violations = []
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            if any(name == "logging" or name.startswith("logging.") for name in modules):
                violations.append(f"{path.name}:{node.lineno}: builtin logging")
            if isinstance(node, ast.Attribute) and node.attr in {
                "getLogger",
                "addFilter",
                "removeFilter",
                "addHandler",
                "removeHandler",
            }:
                violations.append(f"{path.name}:{node.lineno}: logger manipulation")
    assert violations == []
