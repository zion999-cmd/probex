"""SC-9：Replay 与存储层的依赖隔离。

`market/replay` 与 `storage/events` 只允许依赖 `market/events`、`storage/events`
与标准库；不得 import Feature / Jev / Strategy / Execution 等领域。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAYER_DIRS = (
    PROJECT_ROOT / "market" / "replay",
    PROJECT_ROOT / "storage" / "events",
)

#: 允许出现的顶层依赖。
ALLOWED_ROOTS = {
    "__future__",
    "abc",
    "collections",
    "dataclasses",
    "enum",
    "hashlib",
    "json",
    "pathlib",
    "typing",
    "market",
    "storage",
}

#: 明确禁止的下游领域。
FORBIDDEN_ROOTS = {
    "accounting",
    "execution",
    "features",
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


class ReplayLayerIsolationTest(unittest.TestCase):
    def test_scan_covers_the_layer(self) -> None:
        files = _layer_files()
        self.assertGreaterEqual(len(files), 6, "隔离扫描必须覆盖 replay 与 storage 模块")

    def test_no_forbidden_domain_imports(self) -> None:
        for path in _layer_files():
            roots = _imported_roots(path)
            with self.subTest(module=str(path.relative_to(PROJECT_ROOT))):
                self.assertEqual(roots & FORBIDDEN_ROOTS, set())

    def test_only_events_and_stdlib_dependencies(self) -> None:
        for path in _layer_files():
            roots = _imported_roots(path)
            with self.subTest(module=str(path.relative_to(PROJECT_ROOT))):
                self.assertEqual(
                    roots - ALLOWED_ROOTS,
                    set(),
                    msg=f"unexpected dependency introduced in {path.name}",
                )

    def test_replay_layer_does_not_define_a_second_event_type(self) -> None:
        # 不允许另造 ReplayEvent
        for path in _layer_files():
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("class ReplayEvent", source)


if __name__ == "__main__":
    unittest.main()
