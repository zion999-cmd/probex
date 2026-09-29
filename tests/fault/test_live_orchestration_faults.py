"""P0001.9.7 Fault：live loop 的失败行为（SC-6 – SC-13、SC-16 – SC-20、SC-22、SC-28 – SC-31）。

命题：编排不得把"未知"当"没有"，不得在未就绪/失效状态下新增暴露，也不得把降险路径堵死。
"""

from __future__ import annotations

import unittest

from connectors.binance.execution import ExternalFactsUnavailableError, ExecutionOutcomeUnknown
from connectors.binance.market_data.errors import TransportError
from execution.types import ExternalOrder, OrderStatus
from live import OrchestratorState
from market.events.types import Venue
from portfolio.position import Position
from portfolio.types import Side
from readiness.types import RecoveryGeneration
from risk.types import KillSwitchMode
from strategy.maker.types import QuoteAction
from tests.orchestration_support import NOW, OrchestrationStack, context
from tests.strategy_support import make_record, market_state
from tests.support import SYMBOL


def flat_position() -> Position:
    return Position(symbol=SYMBOL, qty=0.0, avg_entry_price=0.0, mark_price=60_000.0)


def long_position(qty: float = 0.001) -> Position:
    return Position(symbol=SYMBOL, qty=qty, avg_entry_price=60_000.0, mark_price=60_000.0)


def record():
    return make_record(now_ms=NOW, ttl_ms=600_000)


def state():
    return market_state(best_bid=60_000.0, best_ask=60_010.0)


class UnavailableFactsTest(unittest.TestCase):
    """SC-7 / SC-8：external facts 不可用 ≠ 空；不可用必须导致"不收敛"。"""

    def test_unavailable_facts_never_converge(self) -> None:
        stack = OrchestrationStack.build(
            facts=type(
                "Broken",
                (),
                {"parse_open_orders": lambda self, symbol: (_ for _ in ()).throw(TransportError("reset")),
                 "parse_recent_fills": lambda self, symbol, since_ms=None: ()},
            )(),
        )

        outcome = stack.orchestrator.reconcile_now(reason="facts_unavailable")

        self.assertFalse(outcome.converged)
        self.assertIn("unavailable", outcome.detail)
        self.assertIs(stack.orchestrator.state, OrchestratorState.RECONCILING)

    def test_true_empty_converges(self) -> None:
        stack = OrchestrationStack.build()

        outcome = stack.orchestrator.reconcile_now(reason="startup")

        self.assertTrue(outcome.converged)  # 供应商真返回空 ⇒ 交易所确认没有挂单
        self.assertIs(stack.orchestrator.state, OrchestratorState.LIVE)


class UnknownOutcomeTest(unittest.TestCase):
    """SC-9 / SC-31：UNKNOWN submit/cancel 触发 reconciliation；未收敛前禁止新增暴露。"""

    def test_unknown_submit_triggers_reconciliation_and_blocks_new_risk(self) -> None:
        stack = OrchestrationStack.build(
            responses={"POST": lambda url: (_ for _ in ()).throw(ExecutionOutcomeUnknown(status=503, detail="x"))}
        )
        stack.orchestrator.run_loop(state=state(), prediction=record())
        self.assertGreaterEqual(stack.adapter.unknown_submit_count, 1)

        # 未确认的 submit ⇒ 本地订单仍 PENDING_CREATE（暴露照常计入，不静默清零）
        self.assertTrue(any(order.status is OrderStatus.PENDING_CREATE for order in stack.tracker.orders))

        reconciliation = stack.orchestrator.reconcile_now(reason="submit_unknown")
        self.assertIsNotNone(reconciliation)

        # reconciliation 后仍不允许新增暴露（PENDING_CREATE 不是终态 ⇒ 仍占暴露）
        posts_before = len([call for call in stack.transport.calls if call[0] == "POST"])
        stack.orchestrator.run_loop(state=state(), prediction=record())
        self.assertEqual(len([call for call in stack.transport.calls if call[0] == "POST"]), posts_before)

    def test_unknown_cancel_does_not_fake_terminal_and_triggers_reconcile(self) -> None:
        stack = OrchestrationStack.build(
            responses={"DELETE": lambda url: (_ for _ in ()).throw(TransportError("cancel response lost"))}
        )
        stack.orchestrator.run_loop(state=state(), prediction=record())

        outcome = stack.orchestrator.run_loop(
            state=market_state(tradeable=False), prediction=record()
        )

        self.assertEqual(outcome.cancelled, ())  # 未确认 ⇒ 不算撤单完成
        self.assertTrue(any("cancel_pending" in note for note in outcome.telemetry.notes))
        self.assertIs(stack.orchestrator.state, OrchestratorState.LIVE)
        reconciliation = stack.orchestrator.reconcile_now(reason="cancel_unknown")
        self.assertIsNotNone(reconciliation)

    def test_lost_order_blocks_new_exposure_until_converged(self) -> None:
        stack = OrchestrationStack.build()
        tracker = stack.tracker
        from risk.types import OrderProposal

        order = tracker.create(
            OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.001, price=60_000.0, post_only=True),
            timestamp=NOW,
        )
        tracker.mark_lost(order.client_order_id, timestamp=NOW, reason="ack timeout")

        stack.orchestrator.run_loop(state=state(), prediction=record())

        self.assertEqual(stack.transport.calls, [])
        self.assertGreater(tracker.uncertain_exposure(), 0.0)


class DiscontinuityTest(unittest.TestCase):
    """SC-11 / SC-18：断线 / skipped transition / 未就绪 ⇒ 零 submit（cancel 仍可行）。"""

    def test_discontinuity_invalidates_authority_and_blocks_submit(self) -> None:
        stack = OrchestrationStack.build()
        stack.orchestrator.authority_provider = lambda: context(
            recovery_generation=RecoveryGeneration(1, 0)
        )

        outcome = stack.orchestrator.run_loop(state=state(), prediction=record())

        self.assertEqual(stack.transport.calls, [])
        self.assertTrue(any("authority_invalid" in note for note in outcome.telemetry.notes))

    def test_mark_not_ready_blocks_all_submits(self) -> None:
        stack = OrchestrationStack.build()
        stack.orchestrator.mark_not_ready()

        stack.orchestrator.run_loop(state=state(), prediction=record())

        self.assertEqual(stack.transport.calls, [])
        self.assertIs(stack.orchestrator.state, OrchestratorState.NOT_READY)

    def test_skipped_transition_is_visible_and_reconciliation_can_be_triggered(self) -> None:
        """SC-18：被跳过的 stream transition 必须可观测（并可由调用方触发 reconciliation）。"""
        from connectors.binance.private.events import OrderUpdateObservation

        stack = OrchestrationStack.build()
        stack.orchestrator.run_loop(state=state(), prediction=record())
        client_order_id = stack.tracker.orders[0].client_order_id
        stack.tracker.on_event(
            __import__("execution.events", fromlist=["OrderCanceled"]).OrderCanceled(
                client_order_id=client_order_id, timestamp=NOW + 5
            )
        )

        observation = OrderUpdateObservation(
            event_ts=NOW + 10, transaction_ts=NOW + 10, receive_ts=NOW + 11, process_ts=NOW + 11,
            symbol=SYMBOL, client_order_id=client_order_id, order_id=1, side="BUY", order_type="LIMIT",
            execution_type="NEW", order_status="NEW", last_fill_quantity=0.0,
            cumulative_fill_quantity=0.0, last_fill_price=0.0, average_price=0.0, commission=0.0,
            commission_asset="USDT", trade_id=0, is_maker=True, original_quantity=0.001,
            original_price=60_000.0, reduce_only=False,
        )
        stack.orchestrator.bridge_stream_event(observation)

        self.assertGreaterEqual(stack.adapter.skipped_transition_count, 1)
        outcome = stack.orchestrator.reconcile_now(reason="skipped_transition")
        self.assertEqual(outcome.triggered_by, "skipped_transition")


class UserStreamBridgeTest(unittest.TestCase):
    """SC-16 / SC-17：user stream 只经 adapter 桥接；重复事件不重复记账。"""

    def _observation(self, **overrides: object):
        from connectors.binance.private.events import OrderUpdateObservation

        values: dict[str, object] = {
            "event_ts": NOW + 5, "transaction_ts": NOW + 5, "receive_ts": NOW + 6, "process_ts": NOW + 6,
            "symbol": SYMBOL, "client_order_id": "probex-s1-000001", "order_id": 4242, "side": "BUY",
            "order_type": "LIMIT", "execution_type": "TRADE", "order_status": "PARTIALLY_FILLED",
            "last_fill_quantity": 0.001, "cumulative_fill_quantity": 0.001, "last_fill_price": 60_000.0,
            "average_price": 60_000.0, "commission": 0.02, "commission_asset": "USDT", "trade_id": 777,
            "is_maker": True, "original_quantity": 0.001, "original_price": 60_000.0, "reduce_only": False,
        }
        values.update(overrides)
        return OrderUpdateObservation(**values)  # type: ignore[arg-type]

    def test_duplicate_stream_fill_is_accounted_once(self) -> None:
        stack = OrchestrationStack.build()
        stack.orchestrator.run_loop(state=state(), prediction=record())
        client_order_id = stack.tracker.orders[0].client_order_id

        for _ in range(2):  # 同一条事实重复抵达
            stack.orchestrator.bridge_stream_event(self._observation(client_order_id=client_order_id))
            # position 由 accounting 派生（不传）⇒ 与刚应用的事件自洽
            stack.orchestrator.run_loop(state=state(), prediction=record())

        self.assertEqual(len(stack.accounting.fills.fills), 1)
        self.assertAlmostEqual(stack.tracker.require_order(client_order_id).filled_quantity, 0.001)

    def test_stream_events_never_touch_tracker_directly(self) -> None:
        """SC-16：orchestrator 只通过 adapter bridge → poll → engine.on_events。"""
        source = (__import__("pathlib").Path("live") / "orchestrator.py").read_text(encoding="utf-8")

        self.assertIn("self.adapter.bridge_user_event", source)
        self.assertIn("self.adapter.poll()", source)
        self.assertIn("self.engine.on_events", source)
        self.assertNotIn("tracker.on_event", source)  # 不绕过 execution 层


class StopFaultTest(unittest.TestCase):
    """SC-21 / SC-22：stop 撤净挂单；绝不自动平仓。"""

    def test_stop_with_unknown_cancel_reports_remaining(self) -> None:
        stack = OrchestrationStack.build(
            responses={"DELETE": lambda url: (_ for _ in ()).throw(TransportError("cancel lost"))}
        )
        stack.orchestrator.run_loop(state=state(), prediction=record())

        report = stack.orchestrator.stop(position_qty=0.0)

        self.assertTrue(report.remaining_active)  # 未确认 ⇒ 如实报告仍有 active
        self.assertIn("ACTIVE_ORDERS_REMAIN", report.notes)
        self.assertFalse(report.position_remains)

    def test_stop_reports_position_without_flattening(self) -> None:
        stack = OrchestrationStack.build()
        stack.orchestrator.run_loop(state=state(), prediction=record())

        report = stack.orchestrator.stop(position_qty=0.002)

        self.assertTrue(report.position_remains)
        self.assertIn("POSITION_REMAINS", report.notes)
        self.assertFalse(any("MARKET" in query for query in stack.transport.seen_queries))


if __name__ == "__main__":
    unittest.main()
