"""Check protocol independence without importing AstrBot."""

import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for path in (root / "grok_oauth").rglob("*.py"):
    for node in ast.walk(ast.parse(path.read_text())):
        names = []
        if isinstance(node, ast.Import):
            names = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        if any(name.startswith(("astrbot", "astrbot_adapter")) for name in names):
            raise SystemExit(f"Forbidden host import: {path.name}:{node.lineno}")
print("Protocol architecture check passed")
