"""P0001.16 §17/§19 + §15：单一订单写路径守卫 + UNKNOWN 受控故障注入验收（离线）。

§17/§19：唯一写路径必须是
`Risk → Readiness → ExecutionEngine → PrivateExecutionConnector → Binance`；
Strategy / Product / UI / Assistant / ActionGateway **不得**直接调用 connector 或 REST 写方法。

§15：ambiguous submit（响应丢失）必须
- 判为 UNKNOWN（不是 REJECTED / ACCEPTED）；
- **不**重试 submit；
- OrderTracker 不假装终态；
- 通过 REST query 收敛为真实状态；
- reconciliation evidence 可见（trace / product facts）。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from connectors.binance.execution.rest import ExecutionOutcomeUnknown
from portfolio.types import Side
from risk.types import OrderProposal
from tests.execution_support_live import FakeExecutionFetcher, echoing_ack
from tests.integration.test_testnet_product_facts import _stack, _rules
from tests.support import SYMBOL, BASE_TS

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 唯一允许出现"写 REST 方法"的层（connector/adapter 实现层 + acceptance capability）
WRITE_ALLOWED_ROOTS = {"connectors"}
#: 写方法名（唯一真实写请求入口）
WRITE_METHODS = ("submit_post_only_limit", "submit_ioc_limit", "cancel_order")


def _roots(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class SingleWritePathGuardTest(unittest.TestCase):
    def test_only_connector_layer_touches_the_write_rest_methods(self) -> None:
        offenders: list[str] = []
        for root in ("strategy", "product", "assistant", "actions", "execution", "risk", "runtime",
                     "api", "ui", "domain", "venue"):
            for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
                if "__pycache__" in str(path):
                    continue
                source = path.read_text(encoding="utf-8")
                if any(f".{method}(" in source for method in WRITE_METHODS):
                    offenders.append(f"{root}/{path.name}")
        self.assertEqual(offenders, [], f"write REST methods must only appear in {sorted(WRITE_ALLOWED_ROOTS)}")

    def test_product_and_strategy_never_import_connector_implementations(self) -> None:
        for root in ("strategy", "product", "assistant", "actions"):
            for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
                with self.subTest(module=f"{root}/{path.name}"):
                    self.assertNotIn("connectors", _roots(path))

    def test_action_gateway_has_no_trading_write_action(self) -> None:
        """§17：ActionGateway 不能形成第二交易写路径；CAPITAL 仍 unavailable_by_design。"""
        import actions.manifest as manifest_module

        specs = next(value for name, value in vars(manifest_module).items()
                     if name.isupper() and isinstance(value, tuple) and value
                     and hasattr(value[0], "action_id"))
        for spec in specs:
            with self.subTest(action=spec.action_id):
                # 不允许任何"直接下单/撤单/改仓"型 action（唯一写路径只能由引擎驱动）
                self.assertNotIn(spec.action_id, ("trade.submit", "trade.cancel", "orders.submit",
                                                  "execution.submit", "execution.cancel"))
                self.assertFalse(bool(getattr(spec, "mutates_state", False)))

    def test_engine_only_reaches_the_connector(self) -> None:
        engine = (PROJECT_ROOT / "execution" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("self.manager.submit(", engine)
        self.assertNotIn("rest.submit_", engine)


class UnknownFaultInjectionTest(unittest.TestCase):
    """§15：受控故障注入（response lost）——不得靠真实 venue 制造危险状态。"""

    def test_unknown_submit_is_not_retried_and_converges_via_query(self) -> None:
        stack = _stack()
        fetcher = FakeExecutionFetcher(responses={"POST": ExecutionOutcomeUnknown(status=None, detail="dropped"),
                                                  "GET": echoing_ack})
        stack.adapter.rest = __import__("tests.execution_support_live",
                                        fromlist=["client"]).client(fetcher, now_ms=BASE_TS + 1_000)  # type: ignore[attr-defined]
        proposal = OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True)

        result = stack.submit(proposal)
        client_order_id = result.order.client_order_id

        # UNKNOWN：既不是 ACCEPTED 也不是 REJECTED；本地不假装终态
        self.assertEqual(str(stack.adapter.last_submit_outcome.classification.value), "UNKNOWN")
        self.assertEqual(stack.adapter.unknown_submit_count, 1)
        self.assertEqual(len(fetcher.calls), 1)                                  # **没有 retry**
        self.assertEqual(stack.tracker.require_order(client_order_id).status.value, "PENDING_CREATE")

        # REST 仅用于 query/reconciliation ⇒ 收敛为真实状态
        record = stack.resolve_unknown(client_order_id)
        self.assertEqual(record["outcome"], "reconciled_from_query")
        self.assertEqual(stack.tracker.require_order(client_order_id).status.value, "OPEN")
        self.assertEqual(len(fetcher.calls), 2)                                  # 只多了一次 GET query
        evidence = stack.reconciliation_facts()
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].identity_kind, "client_order_id")
        self.assertIn("reconciled_from_query", evidence[0].outcome)

    def test_unknown_without_venue_record_stays_unknown_and_is_not_fabricated(self) -> None:
        stack = _stack()
        # POST 丢失 + query 也返回 unknown ⇒ 保持 UNKNOWN（LOST + unresolved），不伪造终态
        fetcher = FakeExecutionFetcher(responses={
            "POST": ExecutionOutcomeUnknown(status=None, detail="dropped"),
            "GET": ExecutionOutcomeUnknown(status=None, detail="no order record")})
        stack.adapter.rest = __import__("tests.execution_support_live",
                                        fromlist=["client"]).client(fetcher, now_ms=BASE_TS + 1_000)  # type: ignore[attr-defined]
        result = stack.submit(OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=60_000.0,
                                            post_only=True))
        client_order_id = result.order.client_order_id

        record = stack.resolve_unknown(client_order_id)
        self.assertIn(record["outcome"], ("still_unknown_no_venue_record", "still_unknown_query_failed"))
        order = stack.tracker.require_order(client_order_id)
        self.assertIn(order.status.value, ("PENDING_CREATE", "LOST"))
        self.assertNotIn(order.status.value, ("FILLED", "CANCELED", "FAILED", "OPEN"))
        self.assertTrue(stack.tracker.unresolved_orders() or order.status.value == "LOST")

    def test_unknown_reason_is_visible_in_product_facts(self) -> None:
        from runtime.testnet import build_testnet_product_service

        stack = _stack()
        fetcher = FakeExecutionFetcher(responses={
            "POST": ExecutionOutcomeUnknown(status=None, detail="dropped"),
            "GET": ExecutionOutcomeUnknown(status=None, detail="no order record")})
        stack.adapter.rest = __import__("tests.execution_support_live",
                                        fromlist=["client"]).client(fetcher, now_ms=BASE_TS + 1_000)  # type: ignore[attr-defined]
        result = stack.submit(OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=60_000.0,
                                            post_only=True))
        stack.resolve_unknown(result.order.client_order_id)
        snapshot = build_testnet_product_service(stack).snapshot()

        self.assertEqual(snapshot.execution.last_submit_classification.value, "UNKNOWN")
        self.assertTrue(snapshot.execution.last_submit_reason.known)
        self.assertIn("ExecutionOutcomeUnknown", str(snapshot.execution.last_submit_reason.value))
        stages = [entry for entry in snapshot.evidence.trace if entry.stage == "reconciliation"]
        self.assertTrue(stages)
        self.assertIn("still_unknown", str(stages[-1].outcome))


if __name__ == "__main__":
    unittest.main()
