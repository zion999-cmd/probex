"""SC-11：P0001.3 层的依赖隔离。

扫描 `market/features` 与 `market/state`：只允许标准库与 `market` 自身的依赖；
不得 import Jev / Strategy / Execution / Portfolio / Risk（以及任何交易侧领域）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAYER_DIRS = (
    PROJECT_ROOT / "market" / "features",
    PROJECT_ROOT / "market" / "state",
)

STDLIB_ROOTS = {
    "__future__",
    "bisect",
    "collections",
    "dataclasses",
    "math",
    "typing",
}
LAYER_ROOTS = {"market"}

FORBIDDEN_ROOTS = {
    "accounting",
    "execution",
    "jev",
    "maker",
    "paper",
    "policy",
    "portfolio",
    "prediction",
    "reports",
    "risk",
    "simulation",
    "strategy",
}


def _layer_files() -> list[Path]:
    return sorted(path for directory in LAYER_DIRS for path in directory.glob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class FeatureLayerIsolationTest(unittest.TestCase):
    def test_scan_covers_the_layer(self) -> None:
        self.assertGreaterEqual(len(_layer_files()), 10)

    def test_sc11_no_trading_domain_imports(self) -> None:
        for path in _layer_files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) & FORBIDDEN_ROOTS, set())

    def test_layer_only_depends_on_market_and_stdlib(self) -> None:
        allowed = STDLIB_ROOTS | LAYER_ROOTS
        for path in _layer_files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) - allowed, set())

    def test_layer_does_not_depend_on_storage_or_connectors(self) -> None:
        for path in _layer_files():
            roots = _imported_roots(path)
            with self.subTest(module=path.name):
                self.assertNotIn("storage", roots)
                self.assertNotIn("connectors", roots)


if __name__ == "__main__":
    unittest.main()
