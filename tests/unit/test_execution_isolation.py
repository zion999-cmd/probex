"""SC-16：Execution 层的依赖与 wall-clock 隔离。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_DIR = PROJECT_ROOT / "execution"

STDLIB_ROOTS = {"__future__", "collections", "dataclasses", "decimal", "enum", "math", "typing"}
LAYER_ROOTS = {"execution", "market", "portfolio", "risk"}

FORBIDDEN_ROOTS = {
    "prediction",
    "jev",
    "strategy",
    "policy",
    "connectors",
    "storage",
    "time",
    "datetime",
    "socket",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
}


def _layer_files() -> list[Path]:
    return sorted(EXECUTION_DIR.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class ExecutionIsolationTest(unittest.TestCase):
    def test_scan_covers_the_layer(self) -> None:
        self.assertGreaterEqual(len(_layer_files()), 8)

    def test_sc16_no_prediction_jev_strategy_imports(self) -> None:
        for path in _layer_files():
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                self.assertEqual(_imported_roots(path) & FORBIDDEN_ROOTS, set())

    def test_only_expected_dependencies(self) -> None:
        allowed = STDLIB_ROOTS | LAYER_ROOTS
        for path in _layer_files():
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                self.assertEqual(_imported_roots(path) - allowed, set())

    def test_no_wall_clock(self) -> None:
        for path in _layer_files():
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                for needle in ("time.time", "time.monotonic", "datetime.now", "datetime.utcnow"):
                    self.assertNotIn(needle, source)

    def test_execution_does_not_mutate_accounting_directly(self) -> None:
        """只有 ExecutionEngine 通过 canonical Fill 记账；tracker / manager / adapter 不碰 Accounting。"""
        for name in ("tracker.py", "manager.py", "types.py", "events.py"):
            path = EXECUTION_DIR / name
            with self.subTest(module=name):
                self.assertNotIn("portfolio.accounting", _imported_modules(path))
        engine_source = (EXECUTION_DIR / "engine.py").read_text(encoding="utf-8")
        self.assertIn("portfolio.accounting", _imported_modules(EXECUTION_DIR / "engine.py"))
        self.assertIn("record_fill", engine_source)


def _imported_modules(path: Path) -> set[str]:
    """完整 import 路径集合（用于断言「谁依赖谁」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


if __name__ == "__main__":
    unittest.main()
