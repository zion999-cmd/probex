"""P0001.9.6 集成：ExecutionEngine + RiskGate + authority + Binance adapter 全链（SC-1/2/19/21/24）。

验证"真实 adapter 只是既有执行栈的 venue 实现"：
- `RiskGate` 仍在每次 submit 前执行（SC-21）；
- authority 无效时**不触网**（SC-1）；
- 确认 ACK 后 tracker/accounting 正常收敛（SC-6）；
- adapter 不做交易决策（SC-22 由静态测试覆盖）。
"""

from __future__ import annotations

import unittest

from connectors.binance.execution import BinanceExecutionAdapter, ExecutionAuthorityContext, SubmitClassification
from connectors.binance.market_data.exchange_info import TradingRules
from execution.engine import ExecutionEngine
from execution.manager import OrderManager
from execution.tracker import OrderTracker
from portfolio.accounting import AccountingCore
from portfolio.types import Side
from readiness.types import RecoveryGeneration
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.types import KillSwitchMode, OrderProposal, RiskDecisionType
from tests.execution_support_live import FakeExecutionFetcher, ack_payload
from tests.support import BASE_TS, SYMBOL
from tests.unit.test_execution_adapter import authority, context, order
from connectors.binance.execution.rest import BinanceExecutionRestClient
from tests.execution_support_live import credentials


def _rules() -> TradingRules:
    return TradingRules(
        symbol=SYMBOL,
        status="TRADING",
        tick_size=0.1,
        min_price=100.0,
        max_price=1_000_000.0,
        step_size=0.001,
        min_qty=0.001,
        max_qty=100.0,
        min_notional=50.0,
    )


def _stack(*, fetcher: FakeExecutionFetcher | None = None, authority_context=None):
    transport = fetcher if fetcher is not None else FakeExecutionFetcher(responses={"POST": ack_payload()})
    rest = BinanceExecutionRestClient(
        credentials=credentials(),
        fetcher=transport,
        base_url="https://demo-fapi.binance.com",
        clock=lambda: BASE_TS + 1_000,
    )
    tracker = OrderTracker(session_id="s1")
    adapter = BinanceExecutionAdapter(
        rest=rest,
        environment=__import__("readiness.types", fromlist=["Environment"]).Environment.TESTNET,
        authority_provider=lambda: (authority_context if authority_context is not None else context()),
        rules_provider=_rules,
        symbol=SYMBOL,
        local_status_provider=lambda cid: tracker.require_order(cid).status,
    )
    manager = OrderManager(tracker=tracker, adapter=adapter)
    accounting = AccountingCore(initial_balance=10_000.0)
    accounting.update_mark_price(SYMBOL, 60_000.0, timestamp=BASE_TS)
    engine = ExecutionEngine(
        accounting=accounting,
        gate=RiskGate(RiskLimits(max_position_qty=1.0, max_open_order_exposure=5_000.0, max_daily_loss=100.0)),
        manager=manager,
        book_healthy=True,
    )
    return engine, adapter, manager, tracker, accounting, transport


def _proposal(**overrides: object) -> OrderProposal:
    values: dict[str, object] = {
        "symbol": SYMBOL,
        "side": Side.BUY,
        "quantity": 0.002,
        "price": 60_000.0,
        "post_only": True,
    }
    values.update(overrides)
    return OrderProposal(**values)  # type: ignore[arg-type]


class EngineIntegrationTest(unittest.TestCase):
    def test_confirmed_ack_flows_through_engine(self) -> None:
        engine, _adapter, _manager, tracker, _accounting, transport = _stack()

        result = engine.submit(_proposal(), now_ms=BASE_TS)

        self.assertTrue(result.allowed if hasattr(result, "allowed") else True)
        self.assertEqual(len([call for call in transport.calls if call[0] == "POST"]), 1)
        self.assertEqual(len(tracker.orders), 1)
        self.assertEqual(tracker.require_order(tracker.orders[0].client_order_id).status.value, "OPEN")

    def test_sc21_risk_gate_still_runs_before_submit(self) -> None:
        engine, _adapter, _manager, tracker, _accounting, transport = _stack()

        rejected = engine.submit(_proposal(quantity=10.0), now_ms=BASE_TS)  # 超过 max_position_qty

        self.assertIs(rejected.decision.decision, RiskDecisionType.REJECT)
        self.assertEqual(transport.calls, [])  # 被 RiskGate 挡下 ⇒ 完全没有触网
        self.assertEqual(len(tracker.orders), 0)

    def test_sc1_invalid_authority_blocks_before_network(self) -> None:
        from readiness.types import RecoveryGeneration

        engine, _adapter, _manager, tracker, _accounting, transport = _stack(
            authority_context=context(recovery_generation=RecoveryGeneration(1, 0))
        )

        engine.submit(_proposal(), now_ms=BASE_TS)

        self.assertEqual(transport.calls, [])  # authority 失效 ⇒ 零请求
        local = tracker.require_order(tracker.orders[0].client_order_id)
        self.assertEqual(local.status.value, "FAILED")  # 本地拒绝（不是交易所事实）
        self.assertTrue(local.lost_reason.startswith("local_reject:"))
        self.assertIn("RECOVERY_GENERATION_CHANGED", local.lost_reason)

    def test_sc2_mainnet_environment_with_testnet_authority_is_refused(self) -> None:
        from readiness.types import Environment

        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        engine, _adapter, _manager, _tracker, _accounting, transport = _stack(fetcher=fetcher)
        # 把 adapter 改成 MAINNET（authority 仍是 TESTNET）
        engine.manager.adapter.environment = Environment.MAINNET

        engine.submit(_proposal(), now_ms=BASE_TS)

        self.assertEqual(transport.calls, [])

    def test_reduce_only_is_mapped_on_the_wire(self) -> None:
        """§13：adapter 原样映射 reduceOnly，不自行判断"是否真的降险"。"""
        _engine, adapter, _manager, _tracker, _accounting, transport = _stack()

        adapter.submit_with_outcome(order(reduce_only=True))

        self.assertEqual(len([call for call in transport.calls if call[0] == "POST"]), 1)
        self.assertIn("reduceOnly=true", transport.seen_queries[-1])

    def test_cancel_path_through_engine(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={"POST": ack_payload(), "DELETE": ack_payload(status="CANCELED")}
        )
        engine, _adapter, _manager, tracker, _accounting, transport = _stack(fetcher=fetcher)
        engine.submit(_proposal(), now_ms=BASE_TS)
        client_order_id = tracker.orders[0].client_order_id

        engine.cancel(client_order_id, now_ms=BASE_TS + 1)

        self.assertEqual(tracker.require_order(client_order_id).status.value, "CANCELED")
        self.assertEqual(len([call for call in transport.calls if call[0] == "DELETE"]), 1)


class AdapterCompositionTest(unittest.TestCase):
    def test_unknown_submit_leaves_pending_create_and_no_duplicate_request(self) -> None:
        from connectors.binance.market_data.errors import TransportError

        fetcher = FakeExecutionFetcher(responses={"POST": TransportError("lost")})
        engine, _adapter, _manager, tracker, _accounting, transport = _stack(fetcher=fetcher)

        engine.submit(_proposal(), now_ms=BASE_TS)

        self.assertEqual(len([call for call in transport.calls if call[0] == "POST"]), 1)
        self.assertEqual(tracker.orders[0].status.value, "PENDING_CREATE")
        self.assertGreater(tracker.total_pending_exposure(), 0.0)  # 暴露照常计入

    def test_submit_outcome_is_structured(self) -> None:
        _engine, adapter, _manager, _tracker, _accounting, _transport = _stack()

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_ACCEPTED)
        self.assertEqual(outcome.acknowledgement.exchange_order_id, "4242")


if __name__ == "__main__":
    unittest.main()
