"""SC-6 / SC-11：预测层的依赖与字段隔离。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREDICTION_DIR = PROJECT_ROOT / "prediction"

STDLIB_ROOTS = {
    "__future__",
    "asyncio",
    "collections",
    "dataclasses",
    "enum",
    "hashlib",
    "json",
    "math",
    "typing",
}
LAYER_ROOTS = {"market", "prediction"}

#: 网络与 wall-clock：预测层不得直接依赖（P0001.4 阶段完全没有真实网络出口）。
FORBIDDEN_NETWORK_ROOTS = {
    "aiohttp",
    "http",
    "httpx",
    "requests",
    "socket",
    "ssl",
    "urllib",
    "websocket",
    "websockets",
    "time",
}

#: 下游交易领域：预测层不得依赖（否则概率模型会慢慢变成 Policy）。
FORBIDDEN_DOMAIN_ROOTS = {
    "accounting",
    "execution",
    "maker",
    "paper",
    "policy",
    "portfolio",
    "risk",
    "simulation",
    "strategy",
    "connectors",
    "storage",
}


def _layer_files() -> list[Path]:
    return sorted(PREDICTION_DIR.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _action_identifiers(path: Path) -> list[str]:
    """收集代码中作为标识符或字典键出现的动作词（不含文档字符串）。"""
    actions = {"BUY", "SELL", "HOLD"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in actions:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in actions:
            found.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in actions:
            found.append(node.name)
        elif isinstance(node, ast.arg) and node.arg in actions:
            found.append(node.arg)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value in actions:
                    found.append(str(key.value))
    return found


class PredictionIsolationTest(unittest.TestCase):
    def test_scan_covers_the_layer(self) -> None:
        self.assertGreaterEqual(len(_layer_files()), 10)

    def test_sc11_no_network_or_wall_clock_dependencies(self) -> None:
        for path in _layer_files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) & FORBIDDEN_NETWORK_ROOTS, set())

    def test_sc6_no_trading_domain_dependencies(self) -> None:
        for path in _layer_files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) & FORBIDDEN_DOMAIN_ROOTS, set())

    def test_only_market_prediction_and_stdlib(self) -> None:
        allowed = STDLIB_ROOTS | LAYER_ROOTS
        for path in _layer_files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) - allowed, set())

    def test_sc6_no_action_vocabulary_in_prediction_code(self) -> None:
        for path in _layer_files():
            with self.subTest(module=path.name):
                self.assertEqual(_action_identifiers(path), [])


if __name__ == "__main__":
    unittest.main()
