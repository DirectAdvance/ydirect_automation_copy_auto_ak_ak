"""Characterization tests for the Direct modular-monolith boundaries."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


DIRECT_DIR = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            result.add(module)
            result.update(f"{module}.{alias.name}" for alias in node.names)
    return result


def test_service_entrypoints_do_not_import_web_composition_root():
    for name in (
        "content_main.py",
        "content_worker.py",
        "copy_main.py",
        "worker_main.py",
        "price_check_cron.py",
    ):
        imports = _imports(DIRECT_DIR / name)
        assert "direct.create.blueprint" not in imports, name
        assert "direct.main" not in imports, name


def test_runtime_has_no_route_registration_or_blueprint_object():
    tree = ast.parse((DIRECT_DIR / "core" / "automation_runtime.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("routes_"), node.module
        if isinstance(node, ast.Name):
            assert node.id != "bp"


def test_blueprint_is_a_thin_composition_root():
    path = DIRECT_DIR / "create/blueprint.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert len(path.read_text(encoding="utf-8").splitlines()) < 500
    assert set(functions) <= {"init_direct", "__getattr__", "_wire_copy_routes"}


def test_worker_and_copy_import_without_loading_blueprint():
    code = """
import sys
import direct.copy_main
import direct.worker_main
assert 'direct.create.blueprint' not in sys.modules
assert 'direct.main' not in sys.modules
"""
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=DIRECT_DIR.parent,
        check=True,
        capture_output=True,
        text=True,
    )
