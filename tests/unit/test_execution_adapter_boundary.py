"""P0001.9.6 静态边界测试（SC-22 / SC-23 / SC-30）。

1. `connectors/binance/execution/` **只**是 venue 实现：不 import 交易决策层（strategy/prediction/jev/maker）；
2. 写路径不越过 private 只读纪律：private 层仍无下单端点（P0001.9.2 SC-14 的延伸）；
3. adapter 不做 round：源码里没有 round()/float 格式化数字后再发送取整的行为；
4. 签名/凭据不出现在 repr 或异常构造里。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_DIR = PROJECT_ROOT / "connectors" / "binance" / "execution"
PRIVATE_DIR = PROJECT_ROOT / "connectors" / "binance" / "private"

#: 决策层（adapter 绝不能依赖）；`risk` 只允许数据契约模块。
FORBIDDEN_ROOTS = {"strategy", "prediction", "jev", "policy"}
ALLOWED_RISK_MODULES = {"risk.types", "risk.limits", "risk.high_watermark"}

#: 写路径端点标记（只允许出现在 execution 目录）。
WRITE_MARKERS = ("/fapi/v1/order", "newClientOrderId", "origClientOrderId")


def _files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.py"))


def _imports(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
            modules.add(node.module)
    return roots, modules


class ExecutionAdapterBoundaryTest(unittest.TestCase):
    def test_scan_covers_expected_modules(self) -> None:
        names = {path.name for path in _files(EXECUTION_DIR)}

        self.assertTrue({"__init__.py", "adapter.py", "parsing.py", "rest.py"} <= names)

    def test_sc22_no_decision_layer_imports(self) -> None:
        """只检查**真实依赖**（import 与属性访问），不因为注释里提到某个词就失败。"""
        for path in _files(EXECUTION_DIR):
            roots, modules = _imports(path)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported_names = {
                alias.asname or alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            with self.subTest(module=path.name):
                self.assertEqual(roots & FORBIDDEN_ROOTS, set())
                self.assertTrue({m for m in modules if m.startswith("risk")} <= ALLOWED_RISK_MODULES)
                for forbidden in ("maker_policy", "MakerPolicy", "predict", "Prediction"):
                    self.assertNotIn(forbidden, imported_names)

    def test_sc23_no_rounding_on_the_write_path(self) -> None:
        """§14：**写线格式层**不得做任何 round（不合法就拒绝，不偷偷改值）。"""
        source = (EXECUTION_DIR / "rest.py").read_text(encoding="utf-8")

        self.assertNotIn("round(", source)
        self.assertNotIn("Decimal(", source)
        self.assertNotIn(":0.4f", source)
        # adapter 层唯一的 round( 只允许用于**步长校验**（_is_multiple），不得用于改写订单值
        adapter_source = (EXECUTION_DIR / "adapter.py").read_text(encoding="utf-8")
        self.assertEqual(adapter_source.count("round("), 1)
        self.assertIn("def _is_multiple", adapter_source)

    def test_write_endpoints_are_isolated_from_private_layer(self) -> None:
        """SC-24：private 层保持只读；写标记只允许在 execution 目录。"""
        for path in _files(PRIVATE_DIR):
            source = path.read_text(encoding="utf-8")
            for marker in WRITE_MARKERS:
                with self.subTest(module=path.name, marker=marker):
                    self.assertNotIn(marker, source)
        execution_source = "\n".join(path.read_text(encoding="utf-8") for path in _files(EXECUTION_DIR))
        self.assertIn("/fapi/v1/order", execution_source)

    def test_only_the_three_allowed_write_calls_exist(self) -> None:
        """写路径只暴露 submit / cancel / query（没有 batch / allOpenOrders / leverage 等）。"""
        source = "\n".join(path.read_text(encoding="utf-8") for path in _files(EXECUTION_DIR))

        for forbidden in ("batchOrders", "allOpenOrders", "leverage", "marginType", "positionSide=LONG", "positionSide=SHORT", "modifyOrder"):
            with self.subTest(marker=forbidden):
                self.assertNotIn(forbidden, source)

    def test_no_credentials_or_signature_in_repr(self) -> None:
        from connectors.binance.execution.rest import BinanceExecutionRestClient, ExecutionRequestRejected
        from tests.execution_support_live import credentials

        client = BinanceExecutionRestClient(
            credentials=credentials(),
            fetcher=__import__("tests.execution_support_live", fromlist=["FakeExecutionFetcher"]).FakeExecutionFetcher(),
            base_url="https://demo-fapi.binance.com",
        )
        rendered = f"{client!r} {client!s} {ExecutionRequestRejected(status=400, code=-2010, message='x')!r}"

        self.assertNotIn("test-execution-secret", rendered)
        self.assertNotIn("signature=", rendered)


if __name__ == "__main__":
    unittest.main()
