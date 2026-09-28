"""SC-6 / SC-11：预测层的依赖、字段与 wall-clock 隔离。

规则（P0001.4 + P0001.4.1）：

- `prediction/**` 中**只有被授权的 transport 模块**允许使用真实网络（标准库 `urllib`）与
  wall-clock（仅用于 latency telemetry）：`providers/systemone.py`（P0001.4.2 热路径）与
  `providers/openrouter.py`（P0001.4.1 已废弃的实验路径）。其余模块一律禁止。
- 不得依赖 Strategy / Execution / Portfolio / Risk / Accounting / storage / connectors。
"""

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

#: 被授权进行真实网络调用的模块（transport 层）。
TRANSPORT_MODULES = {"openrouter.py", "systemone.py"}

#: 该模块额外允许的依赖：环境变量、wall-clock（仅 latency）、真实网络出口。
TRANSPORT_EXTRA_ROOTS = {"os", "time", "urllib"}

#: 明确禁止的网络客户端（只用标准库 urllib，不新增依赖）。
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
}

WALL_CLOCK_ROOT = "time"

#: 下游交易领域。
FORBIDDEN_DOMAIN_ROOTS = {
    "accounting",
    "connectors",
    "execution",
    "maker",
    "paper",
    "policy",
    "portfolio",
    "risk",
    "simulation",
    "storage",
    "strategy",
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


class PredictionIsolationTest(unittest.TestCase):
    def test_scan_covers_the_layer(self) -> None:
        self.assertGreaterEqual(len(_layer_files()), 10)

    def test_sc6_no_trading_domain_dependencies(self) -> None:
        for path in _layer_files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) & FORBIDDEN_DOMAIN_ROOTS, set())

    def test_only_market_prediction_and_stdlib(self) -> None:
        allowed = STDLIB_ROOTS | LAYER_ROOTS
        for path in _layer_files():
            if path.name in TRANSPORT_MODULES:
                continue
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) - allowed, set())

    def test_only_transport_module_is_allowed_network_and_wall_clock(self) -> None:
        for path in _layer_files():
            if path.name in TRANSPORT_MODULES:
                continue
            roots = _imported_roots(path)
            with self.subTest(module=path.name):
                self.assertNotIn(WALL_CLOCK_ROOT, roots, "只有 OpenRouter transport 可以使用 wall-clock")
                self.assertEqual(roots & FORBIDDEN_NETWORK_ROOTS, set(), "只有 OpenRouter transport 可以联网")

    def test_transport_module_uses_only_authorized_extras(self) -> None:
        transport = [path for path in _layer_files() if path.name in TRANSPORT_MODULES]
        self.assertTrue(transport, "必须存在被授权的 transport 模块")

        allowed = STDLIB_ROOTS | LAYER_ROOTS | TRANSPORT_EXTRA_ROOTS
        for path in transport:
            roots = _imported_roots(path)
            with self.subTest(module=path.name):
                self.assertEqual(roots - allowed, set())
                self.assertLessEqual(roots & FORBIDDEN_NETWORK_ROOTS, {"urllib"})

    def test_sc6_no_action_vocabulary_in_prediction_code(self) -> None:
        for path in _layer_files():
            with self.subTest(module=path.name):
                self.assertEqual(_action_identifiers(path), [])

    def test_sc11_no_network_in_replay_facing_layers(self) -> None:
        # market/state 与 market/features 不得因为 P0001.4.1 而获得网络能力
        for directory in (PROJECT_ROOT / "market" / "features", PROJECT_ROOT / "market" / "state"):
            for path in sorted(directory.glob("*.py")):
                roots = _imported_roots(path)
                with self.subTest(module=f"{directory.name}/{path.name}"):
                    self.assertEqual(roots & FORBIDDEN_NETWORK_ROOTS, set())
                    self.assertNotIn(WALL_CLOCK_ROOT, roots)


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


if __name__ == "__main__":
    unittest.main()
