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
#: 禁止的依赖（交易侧运行时 / 输出通道）。
#:
#: P0001.9.3 授权 private 层承担 recovery 编排，因此**仅放行**交易侧的**数据契约 / 已授权算法**；
#: 其余（risk / strategy / prediction / storage / 输出通道 / execution 的运行时模块）仍然禁止。
FORBIDDEN_ROOTS = {"prediction", "risk", "strategy", "storage", "logging", "warnings", "sys"}
ALLOWED_EXECUTION_MODULES = {"execution.types", "execution.tracker", "execution.reconciliation"}
ALLOWED_PORTFOLIO_MODULES = {"portfolio.types", "portfolio.accounting"}
#: 允许的 import 根（标准库 + 本仓库的两层）。
ALLOWED_ROOTS = {
    "__future__",
    "execution",
    "portfolio",
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


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


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
            {"__init__.py", "account.py", "auth.py", "errors.py", "events.py", "orders.py",
             "positions.py", "recovery.py", "rest.py", "runtime.py", "telemetry.py", "trades.py",
             "user_stream.py"} <= names
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

    def test_only_authorized_execution_and_portfolio_modules(self) -> None:
        """P0001.9.3：private 层只允许上述数据契约/已授权算法，不得依赖执行运行时。"""
        for path in _files():
            modules = _imported_modules(path)
            with self.subTest(module=path.name):
                execution = {m for m in modules if m.split(".")[0] == "execution"}
                portfolio = {m for m in modules if m.split(".")[0] == "portfolio"}
                self.assertTrue(
                    execution <= ALLOWED_EXECUTION_MODULES, f"{path.name}: unexpected execution imports {sorted(execution)}"
                )
                self.assertTrue(
                    portfolio <= ALLOWED_PORTFOLIO_MODULES, f"{path.name}: unexpected portfolio imports {sorted(portfolio)}"
                )

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

    def test_recovery_exposes_no_order_operations(self) -> None:
        """P0001.9.3 SC-11：recovery 编排只做只读事实读取与本地状态收敛。"""
        recovery = __import__("connectors.binance.private.recovery", fromlist=["StartupRecovery"])
        public = [name for name in dir(recovery.StartupRecovery) if not name.startswith("_")]

        for forbidden in ("submit", "cancel", "place_order", "amend", "set_leverage"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, public)
        self.assertTrue({"run", "fetch_snapshot", "invalidate"} <= set(public))

    def test_recovery_rest_queries_are_read_only(self) -> None:
        """三只读端点必须都是 GET（无任何写动词）。"""
        from tests.private_support import SYMBOL, FakeRestFetcher, credentials
        from connectors.binance.private.rest import PrivateRestClient

        fetcher = FakeRestFetcher(
            responses={
                "/fapi/v1/openOrders": [],
                "/fapi/v1/allOrders": [],
                "/fapi/v1/userTrades": [],
            }
        )
        client = PrivateRestClient(credentials=credentials(), fetcher=fetcher)

        client.open_orders(SYMBOL)
        client.order_history(SYMBOL, limit=10)
        client.user_trades(SYMBOL, limit=10)

        self.assertEqual(
            fetcher.calls,
            [("GET", "/fapi/v1/openOrders"), ("GET", "/fapi/v1/allOrders"), ("GET", "/fapi/v1/userTrades")],
        )

    def test_sensitive_helpers_exist_for_redaction(self) -> None:
        auth = __import__("connectors.binance.private.auth", fromlist=["sanitize_query", "sanitize_mapping"])

        self.assertEqual(auth.sanitize_query("a=1&signature=deadbeef"), "a=1&signature=***")
        self.assertEqual(auth.sanitize_mapping({"listenKey": "x"})["listenKey"], "***")


if __name__ == "__main__":
    unittest.main()
