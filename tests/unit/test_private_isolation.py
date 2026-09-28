"""P0001.9.2 SC-2 / SC-14：私有层的静态安全与范围约束。

1. 凭据相关标识只出现在允许的模块（`auth.py` / `rest.py`）；
2. 私有层没有任何裸 `print` / `logging` / `warnings` 输出（避免凭据外泄路径）；
3. **没有任何下单/撤单/杠杆/持仓模式端点**（SC-14）；
4. 私有层不 import 交易侧领域（execution / portfolio / risk / strategy）—— 只产出事实。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DIR = PROJECT_ROOT / "connectors" / "binance" / "private"

#: 允许出现 secret / api key 字样的模块（凭据边界）。
CREDENTIAL_MODULES = {"auth.py", "rest.py"}
#: SC-14：禁止出现的端点路径 / 动词组合。
FORBIDDEN_ENDPOINT_MARKERS = (
    "/fapi/v1/order",
    "/fapi/v1/batchOrders",
    "/fapi/v1/allOpenOrders",
    "/fapi/v1/leverage",
    "/fapi/v1/marginType",
    "/fapi/v1/positionSide",
    "/fapi/v1/multiAssetsMargin",
    "cancelOrder",
    "placeOrder",
)
#: 禁止的依赖（交易侧领域 / 输出通道）。
FORBIDDEN_ROOTS = {"execution", "portfolio", "prediction", "risk", "strategy", "logging", "warnings", "sys"}
#: 允许的 import 根（标准库 + 本仓库的两层）。
ALLOWED_ROOTS = {
    "__future__",
    "collections",
    "dataclasses",
    "enum",
    "functools",
    "hashlib",
    "hmac",
    "json",
    "math",
    "os",
    "statistics",
    "time",
    "typing",
    "urllib",
    "market",
    "connectors",
}


def _files() -> list[Path]:
    return sorted(PRIVATE_DIR.glob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class PrivateIsolationTest(unittest.TestCase):
    def test_scan_covers_the_expected_modules(self) -> None:
        names = {path.name for path in _files()}

        self.assertTrue(
            {"__init__.py", "account.py", "auth.py", "errors.py", "events.py", "positions.py",
             "rest.py", "runtime.py", "telemetry.py", "user_stream.py"} <= names
        )

    def test_sc2_secret_handling_only_in_credential_modules(self) -> None:
        """secret 的读写只允许在凭据边界模块内；其他模块最多只能提示环境变量名。"""
        for path in _files():
            source = path.read_text(encoding="utf-8")
            for marker in ("api_secret", "_api_secret"):
                with self.subTest(module=path.name, marker=marker):
                    if path.name in CREDENTIAL_MODULES:
                        continue
                    self.assertNotIn(marker, source)

    def test_only_the_auth_module_reads_the_environment(self) -> None:
        """`os.environ` 只允许出现在 auth.py（唯一读取凭据的地方）。"""
        readers = [
            path.name
            for path in _files()
            if "os.environ" in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(readers, ["auth.py"])

    def test_sc2_no_print_or_logging_paths(self) -> None:
        for path in _files():
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("print(", source)
                self.assertNotIn("logger", source)
                self.assertNotIn("logging.", source)

    def test_sc14_no_order_or_configuration_endpoints(self) -> None:
        for path in _files():
            source = path.read_text(encoding="utf-8")
            for marker in FORBIDDEN_ENDPOINT_MARKERS:
                with self.subTest(module=path.name, marker=marker):
                    self.assertNotIn(marker, source)

    def test_no_trading_layer_dependencies(self) -> None:
        for path in _files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) & FORBIDDEN_ROOTS, set())

    def test_only_expected_import_roots(self) -> None:
        for path in _files():
            with self.subTest(module=path.name):
                self.assertEqual(_imported_roots(path) - ALLOWED_ROOTS, set())

    def test_runtime_exposes_no_order_operations(self) -> None:
        runtime = __import__("connectors.binance.private.runtime", fromlist=["PrivateAccountRuntime"])
        public = [name for name in dir(runtime.PrivateAccountRuntime) if not name.startswith("_")]

        self.assertNotIn("submit", public)
        self.assertNotIn("cancel", public)
        self.assertNotIn("place_order", public)
        self.assertTrue({"start", "stop", "pump_once", "refresh_snapshot", "telemetry"} <= set(public))

    def test_sensitive_helpers_exist_for_redaction(self) -> None:
        auth = __import__("connectors.binance.private.auth", fromlist=["sanitize_query", "sanitize_mapping"])

        self.assertEqual(auth.sanitize_query("a=1&signature=deadbeef"), "a=1&signature=***")
        self.assertEqual(auth.sanitize_mapping({"listenKey": "x"})["listenKey"], "***")


if __name__ == "__main__":
    unittest.main()
