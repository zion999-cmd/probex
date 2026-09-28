"""SC-13：策略层的依赖隔离（不联网、不使用 wall-clock、不触碰执行/预测运行时内部）。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_DIR = PROJECT_ROOT / "strategy"

STDLIB_ROOTS = {"__future__", "dataclasses", "enum", "math", "typing"}
LAYER_ROOTS = {"strategy", "market", "portfolio", "prediction", "risk", "execution"}

#: 这些模块属于「运行时 / 适配器」，策略层不得依赖（只能依赖 `execution.types` 这类数据契约）。
FORBIDDEN_MODULES = {
    "execution.adapters",
    "execution.engine",
    "execution.manager",
    "execution.reconciliation",
    "execution.tracker",
    "execution.events",
    "prediction.providers",
    "prediction.runtime",
    "prediction.scheduler",
}

FORBIDDEN_ROOTS = {
    "asyncio",
    "connectors",
    "datetime",
    "http",
    "httpx",
    "json",
    "os",
    "random",
    "requests",
    "socket",
    "sqlite3",
    "storage",
    "subprocess",
    "time",
    "urllib",
}


def _layer_files() -> list[Path]:
    return sorted(STRATEGY_DIR.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


class StrategyIsolationTest(unittest.TestCase):
    def test_scan_covers_the_layer(self) -> None:
        self.assertGreaterEqual(len(_layer_files()), 7)

    def test_no_forbidden_import_roots(self) -> None:
        for path in _layer_files():
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                roots = {module.split(".")[0] for module in _imports(path)}
                self.assertEqual(roots & FORBIDDEN_ROOTS, set())

    def test_no_runtime_module_dependencies(self) -> None:
        for path in _layer_files():
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                imports = _imports(path)
                for forbidden in FORBIDDEN_MODULES:
                    self.assertNotIn(forbidden, imports)

    def test_only_expected_dependencies(self) -> None:
        allowed = STDLIB_ROOTS | LAYER_ROOTS
        for path in _layer_files():
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                roots = {module.split(".")[0] for module in _imports(path)}
                self.assertEqual(roots - allowed, set())

    def test_no_wall_clock_calls(self) -> None:
        for path in _layer_files():
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                self.assertNotIn("time.time", source)
                self.assertNotIn("datetime.now", source)

    def test_execution_layer_does_not_import_strategy(self) -> None:
        execution_dir = PROJECT_ROOT / "execution"
        for path in sorted(execution_dir.rglob("*.py")):
            with self.subTest(module=f"{path.parent.name}/{path.name}"):
                roots = {module.split(".")[0] for module in _imports(path)}
                self.assertNotIn("strategy", roots)


if __name__ == "__main__":
    unittest.main()
