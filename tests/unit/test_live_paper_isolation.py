"""P0017.5 §4-B2：实时多标的**模拟**交易路径的结构性隔离守卫。

`runtime/live_multi_symbol.py` 与 `runtime/live_collector_source.py` 只允许组合模拟执行
（MultiSymbolSimulatedAdapter / SimulatedVenue）与只读采集器；不得依赖 Testnet/LIVE 写执行
入口，因此模拟模式不可能因配置/注入错误降级为真实下单。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
LIVE_PAPER_MODULES = [
    PROJECT_ROOT / "runtime" / "live_multi_symbol.py",
    PROJECT_ROOT / "runtime" / "live_collector_source.py",
    PROJECT_ROOT / "runtime" / "multi_symbol_core.py",
]

#: 明确禁止的真实写执行/凭据/testnet 入口
FORBIDDEN_IMPORT_PARTS = (
    "runtime.testnet",
    "runtime import testnet",
    "connectors.binance.execution",
    "connectors.binance.private",
    "connectors.binance.execution_connector",
)

#: 允许的 binance 接触：只读公开行情（采集器/数据源）
ALLOWED_BINANCE_MARKET = (
    "connectors.binance.market_data",
    "connectors.binance.public_source",
)


def _import_root(node: ast.AST) -> str:
    if isinstance(node, ast.Import):
        return node.names[0].name
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return ""


class LivePaperIsolationTest(unittest.TestCase):
    def test_live_paper_modules_do_not_import_write_or_testnet_paths(self) -> None:
        for path in LIVE_PAPER_MODULES:
            with self.subTest(module=path.name):
                self.assertTrue(path.is_file(), f"missing {path}")
                tree = ast.parse(path.read_text(encoding="utf-8"))
                roots = [_import_root(n) for n in ast.walk(tree)]
                roots = [r for r in roots if r]
                for root in roots:
                    for forbidden in FORBIDDEN_IMPORT_PARTS:
                        self.assertFalse(
                            root.startswith(forbidden) or root == forbidden,
                            f"{path.name} must not import {forbidden} (found {root})")

    def test_collector_source_only_touches_readonly_market_collector(self) -> None:
        path = PROJECT_ROOT / "runtime" / "live_collector_source.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots = {_import_root(n) for n in ast.walk(tree)}
        roots.discard("")
        binance_roots = {r for r in roots if r.startswith("connectors.binance")}
        self.assertTrue(binance_roots, "collector source should use the read-only market collector")
        for root in binance_roots:
            self.assertTrue(
                root.startswith(ALLOWED_BINANCE_MARKET),
                f"collector source may only touch read-only market modules, found {root}")

    def test_runner_execution_adapter_is_the_simulated_multi_adapter(self) -> None:
        """运行器构造的执行适配器必须是 MultiSymbolSimulatedAdapter（无真实 broker 缝）。"""
        path = PROJECT_ROOT / "runtime" / "live_multi_symbol.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {_import_root(n) for n in ast.walk(tree)}
        imports.discard("")
        # 执行域只允许 simulation + 抽象 engine/manager/tracker
        execution_roots = {r for r in imports if r.startswith("execution.")}
        for root in execution_roots:
            self.assertTrue(
                root.startswith("execution.simulation")
                or root in ("execution.engine", "execution.manager", "execution.tracker"),
                f"runner may only use simulated execution, found {root}")


if __name__ == "__main__":
    unittest.main()
