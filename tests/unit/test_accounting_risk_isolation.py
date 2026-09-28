"""SC-11 / SC-12：Accounting 与 Risk 的依赖、wall-clock 与职责隔离。

规则：

- `portfolio/**` 与 `risk/**` 不得 import Prediction / Jev / Strategy / Execution（SC-11）。
- 两者不得使用 wall-clock（SC-12：Replay 不依赖真实时间）。
- Risk 不直接读取 mutable accounting 对象：`risk/**` 不得 import `portfolio.accounting`。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAYER_DIRS = (PROJECT_ROOT / "portfolio", PROJECT_ROOT / "risk")

STDLIB_ROOTS = {"__future__", "dataclasses", "enum", "math"}
LAYER_ROOTS = {"portfolio", "risk", "market"}

#: 禁止的下游领域（SC-11）+ 网络（本阶段不联网）。
FORBIDDEN_ROOTS = {
    "prediction",
    "jev",
    "strategy",
    "policy",
    "execution",
    "accounting_engine",
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


class AccountingRiskIsolationTest(unittest.TestCase):
    def test_scan_covers_the_layers(self) -> None:
        files = _layer_files()
        self.assertGreaterEqual(len([path for path in files if path.parent.name == "portfolio"]), 4)
        self.assertGreaterEqual(len([path for path in files if path.parent.name == "risk"]), 4)

    def test_sc11_no_prediction_strategy_execution_imports(self) -> None:
        for path in _layer_files():
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                self.assertEqual(_imported_roots(path) & FORBIDDEN_ROOTS, set())

    def test_sc12_no_wall_clock(self) -> None:
        for path in _layer_files():
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("datetime.now", source)
                self.assertNotIn("time.time", source)
                self.assertNotIn("time.monotonic", source)

    def test_only_expected_dependencies(self) -> None:
        allowed = STDLIB_ROOTS | LAYER_ROOTS
        for path in _layer_files():
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                self.assertEqual(_imported_roots(path) - allowed, set())

    def test_gate_never_reads_mutable_accounting(self) -> None:
        """只有 snapshot builder 可以把 mutable accounting 转成快照；gate / limits / types 不得直接读它。"""
        for name in ("gate.py", "limits.py", "types.py"):
            path = PROJECT_ROOT / "risk" / name
            with self.subTest(module=name):
                self.assertNotIn("portfolio.accounting", path.read_text(encoding="utf-8"))
        self.assertIn("portfolio.accounting", (PROJECT_ROOT / "risk" / "snapshot.py").read_text(encoding="utf-8"))

    def test_risk_consumes_snapshots_only(self) -> None:
        source = (PROJECT_ROOT / "risk" / "gate.py").read_text(encoding="utf-8")
        self.assertIn("RiskSnapshot", source)
        self.assertNotIn("AccountingCore", source)


if __name__ == "__main__":
    unittest.main()
