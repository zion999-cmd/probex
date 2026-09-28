"""P0001.9.1 SC-12 / 依赖纪律：connectors 层的静态约束。

1. 只有 live 边界模块可以 import 网络 / wall-clock / 线程相关模块；
2. 任何模块都不得 import 交易侧领域（portfolio / risk / execution / strategy / prediction）；
3. **不存在任何凭据引用**（无 API Key / listenKey / signature），因此 live smoke 不需要凭据。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MARKET_DATA_DIR = PROJECT_ROOT / "connectors" / "binance" / "market_data"

#: live 边界模块允许的额外 import（其余模块一律禁止）。
BOUNDARY_ALLOWED = {
    "transport.py": {"socket", "ssl", "os", "base64", "hashlib", "urllib"},
    "snapshot.py": {"time", "urllib", "json"},
    "runtime.py": {"time"},
}

SENSITIVE_ROOTS = {"socket", "ssl", "urllib", "time", "threading", "asyncio", "http", "os", "subprocess"}
THIRD_PARTY_ROOTS = {"aiohttp", "httpx", "requests", "websocket", "websockets", "websocket_client"}
TRADING_ROOTS = {"execution", "portfolio", "prediction", "risk", "strategy", "storage"}
ALLOWED_ROOTS = {
    "__future__",
    "base64",
    "collections",
    "dataclasses",
    "enum",
    "hashlib",
    "json",
    "math",
    "typing",
    "market",
    "connectors",
} | SENSITIVE_ROOTS

#: 凭据相关标识（public market data 层必须为零）。
#:
#: 注：`listenKey` 自 P0001.9.2 起允许**只**出现在 `endpoints.py`（私有 tier 的 URL 模板常量），
#: 其余 market_data 模块仍禁止涉及任何凭据/密钥概念（见 `test_listen_key_only_in_endpoints`）。
CREDENTIAL_MARKERS = ("apiKey", "api_key", "secret", "signature", "Authorization")
#: 允许出现 `listenKey` 的模块（仅端点常量）。
LISTEN_KEY_ALLOWED = {"endpoints.py"}


def _files() -> list[Path]:
    return sorted(MARKET_DATA_DIR.glob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class LiveIsolationTest(unittest.TestCase):
    def test_scan_covers_the_modules(self) -> None:
        names = {path.name for path in _files()}

        self.assertTrue({"transport.py", "runtime.py", "snapshot.py", "trades.py", "mark.py", "exchange_info.py", "streams.py", "endpoints.py"} <= names)

    def test_sensitive_imports_are_confined_to_the_live_boundary(self) -> None:
        for path in _files():
            allowed = BOUNDARY_ALLOWED.get(path.name, set())
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) & SENSITIVE_ROOTS - allowed, set())

    def test_no_third_party_dependencies(self) -> None:
        for path in _files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) & THIRD_PARTY_ROOTS, set())

    def test_connectors_do_not_depend_on_trading_layers(self) -> None:
        for path in _files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) & TRADING_ROOTS, set())

    def test_only_expected_import_roots(self) -> None:
        for path in _files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) - ALLOWED_ROOTS, set())

    def test_no_credentials_anywhere_in_the_phase(self) -> None:
        """SC-12：public market data 层没有任何凭据引用 ⇒ 无 API Key 也能完成全部 live smoke。"""
        for path in _files():
            source = path.read_text(encoding="utf-8")
            for marker in CREDENTIAL_MARKERS:
                with self.subTest(module=path.name, marker=marker):
                    self.assertNotIn(marker, source)

    def test_listen_key_only_in_endpoints(self) -> None:
        """`listenKey` 只作为端点常量存在；market data 逻辑不接触 listenKey 概念。"""
        for path in _files():
            if path.name in LISTEN_KEY_ALLOWED:
                continue
            with self.subTest(module=path.name):
                self.assertNotIn("listen", path.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
