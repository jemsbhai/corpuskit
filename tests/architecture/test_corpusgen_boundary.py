"""Enforce the alpha-dependency adapter boundary."""

from __future__ import annotations

import ast
from pathlib import Path


def test_only_corpusgen_adapter_imports_corpusgen() -> None:
    source_root = Path(__file__).parents[2] / "src" / "corpuskit"
    violations: list[str] = []

    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root).as_posix()
        if relative.startswith("adapters/corpusgen/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(
                    alias.name == "corpusgen" or alias.name.startswith("corpusgen.")
                    for alias in node.names
                ):
                    violations.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "corpusgen" or module.startswith("corpusgen."):
                    violations.append(f"{relative}:{node.lineno}")

    assert violations == []
