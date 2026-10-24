"""Dependency-direction and lazy-loading guardrails for study."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import main

ROOT = Path(__file__).parents[1]


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add("study" if node.level else node.module.split(".")[0])
    return roots


def test_domain_uses_only_standard_library() -> None:
    for path in (ROOT / "study" / "domain").glob("*.py"):
        assert imported_roots(path) <= set(sys.stdlib_module_names) | {"study"}


def test_application_has_no_outer_layer_or_io_imports() -> None:
    forbidden = {"os", "pathlib", "sqlite3", "subprocess", "textual"}
    for path in (ROOT / "study" / "application").rglob("*.py"):
        imports = imported_roots(path)
        assert not imports & forbidden, f"forbidden application import in {path}"
        source = path.read_text(encoding="utf-8")
        assert "study.adapters" not in source
        assert "study.presentation" not in source


def test_widgets_do_not_access_adapters_or_system_io() -> None:
    for path in (ROOT / "study" / "presentation" / "textual" / "widgets").glob("*.py"):
        assert not imported_roots(path) & {
            "os",
            "pathlib",
            "sqlite3",
            "subprocess",
        }
        assert "study.adapters" not in path.read_text(encoding="utf-8")


def test_main_registers_study_without_top_level_study_import() -> None:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    top_level_imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert all(
        not (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("study")
        )
        for node in top_level_imports
    )
    args = main.create_parser().parse_args(["study"])
    assert args.func is main.cmd_study


def test_bootstrap_import_does_not_load_ml_frameworks() -> None:
    code = (
        "import sys; import study.bootstrap; "
        "assert 'whisperx' not in sys.modules; assert 'torch' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)
