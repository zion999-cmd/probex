"""P0001.9.7 单元测试：QuoteAction 映射 / 不变量 / telemetry（SC-1 – SC-5、SC-9、SC-11 – SC-13、SC-16、SC-32）。

策略与风险语义本身由 P0001.7 / P0001.5 的既有测试覆盖；这里只验证**编排**。
"""

from __future__ import annotations

import unittest

from connectors.binance.execution import ExecutionOutcomeUnknown
from connectors.binance.market_data.errors import TransportError
from execution.types import OrderStatus
from live import OrchestratorState
from market.events.types import Venue
from portfolio.position import Position
from readiness.types import RecoveryGeneration
from risk.types import KillSwitchMode
from strategy.maker.types import QuoteAction
from tests.execution_support_live import ack_payload
from tests.orchestration_support import NOW, OrchestrationStack, context
from tests.strategy_support import make_record, market_state
from tests.support import BASE_TS, SYMBOL


def flat_position() -> Position:
    return Position(symbol=SYMBOL, qty=0.0, avg_entry_price=0.0, mark_price=60_000.0)


def long_position(qty: float = 0.001) -> Position:
    return Position(symbol=SYMBOL, qty=qty, avg_entry_price=60_000.0, mark_price=60_000.0)


class PlaceTest(unittest.TestCase):
    """SC-1：MakerPolicy 的 PLACE 进入既有 ExecutionEngine。"""

    def test_place_submits_through_engine(self) -> None:
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)

        outcome = stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )

        self.assertEqual(outcome.telemetry.bid_action, QuoteAction.PLACE.value)
        self.assertEqual(outcome.telemetry.ask_action, QuoteAction.PLACE.value)  # 双边报价
        self.assertEqual(len([call for call in stack.transport.calls if call[0] == "POST"]), 2)
        self.assertEqual(len(stack.engine.manager.active_orders), 2)
        self.assertEqual(outcome.telemetry.submit_count, 2)

    def test_place_without_proposal_does_not_submit(self) -> None:
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)

        from types import SimpleNamespace

        # 防御性分支：策略层返回 PLACE 但没有 proposal ⇒ 不得下单（用鸭子类型 stub 绕过类型校验）
        stub = SimpleNamespace(
            decide=lambda **kwargs: SimpleNamespace(
                bid=SimpleNamespace(action=QuoteAction.PLACE, proposal=None, client_order_id=None),
                ask=SimpleNamespace(action=QuoteAction.NONE, proposal=None, client_order_id=None),
                blocked_by=None, detail="",
            )
        )
        stack.orchestrator.policy = stub

        outcome = stack.orchestrator.run_loop(state=market_state(), prediction=record)

        self.assertEqual(stack.transport.calls, [])
        self.assertIn("place_without_proposal", outcome.telemetry.notes)


class KeepTest(unittest.TestCase):
    """SC-2：KEEP 不产生任何 REST write。"""

    def test_keep_produces_no_write(self) -> None:
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        state = market_state(best_bid=60_000.0, best_ask=60_010.0)

        first = stack.orchestrator.run_loop(state=state, prediction=record)
        writes_after_first = list(stack.transport.calls)

        second = stack.orchestrator.run_loop(state=state, prediction=record)

        self.assertIn(second.telemetry.bid_action, {QuoteAction.KEEP.value, QuoteAction.NONE.value})
        self.assertEqual(stack.transport.calls, writes_after_first)  # KEEP 零写
        self.assertEqual(first.telemetry.submit_count, 2)  # 双边各一次


class CancelTest(unittest.TestCase):
    """SC-3 / SC-13 / SC-14 / SC-15：CANCEL 可撤；authority 失效仍可撤；prediction stale 时降险路径可用。"""

    def test_cancel_removes_existing_order(self) -> None:
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        state = market_state(best_bid=60_000.0, best_ask=60_010.0)
        stack.orchestrator.run_loop(state=state, prediction=record)
        client_order_id = stack.engine.manager.active_orders[0].client_order_id

        # market 变不健康 ⇒ 既有增加暴露的报价应被撤（不新增）
        outcome = stack.orchestrator.run_loop(
            state=market_state(tradeable=False), prediction=record
        )

        self.assertEqual(outcome.telemetry.bid_action, QuoteAction.CANCEL.value)
        self.assertIn(client_order_id, outcome.cancelled)
        self.assertEqual(stack.tracker.require_order(client_order_id).status, OrderStatus.CANCELED)

    def test_cancel_still_allowed_when_authority_invalid_even_halt_all(self) -> None:
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        state = market_state(best_bid=60_000.0, best_ask=60_010.0)
        stack.orchestrator.run_loop(state=state, prediction=record)
        client_order_id = stack.engine.manager.active_orders[0].client_order_id
        stack.orchestrator.authority_provider = lambda: context(
            recovery_generation=RecoveryGeneration(1, 0), kill_switch_mode=KillSwitchMode.HALT_ALL
        )

        outcome = stack.orchestrator.run_loop(
            state=market_state(tradeable=False), prediction=record
        )

        self.assertIn(client_order_id, outcome.cancelled)
        self.assertEqual(stack.tracker.require_order(client_order_id).status, OrderStatus.CANCELED)

    def test_prediction_stale_blocks_new_exposure_only(self) -> None:
        """SC-14：prediction 过期 ⇒ 只禁**新增增加暴露**的报价（允许撤/保持既有报价）。"""
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        state = market_state(best_bid=60_000.0, best_ask=60_010.0)
        stack.orchestrator.run_loop(state=state, prediction=record)
        writes_before = [call for call in stack.transport.calls if call[0] == "POST"]

        outcome = stack.orchestrator.run_loop(state=state, prediction=None)

        self.assertNotIn(QuoteAction.PLACE.value, (outcome.telemetry.bid_action, outcome.telemetry.ask_action))
        posts_after = [call for call in stack.transport.calls if call[0] == "POST"]
        self.assertEqual(len(posts_after), len(writes_before))  # 无新增暴露


class ReplaceTest(unittest.TestCase):
    """SC-4：REPLACE 严格 cancel-before-replace。"""

    def test_replace_cancels_before_placing_new_leg(self) -> None:
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )
        self.assertEqual(len(stack.engine.manager.active_orders), 2)  # 双边各一笔

        # 价格明显移动 ⇒ REPLACE；第一轮只撤，不改在同一轮假设 cancel 成功
        outcome = stack.orchestrator.run_loop(
            state=market_state(best_bid=60_500.0, best_ask=60_510.0), prediction=record
        )

        self.assertEqual(outcome.telemetry.bid_action, QuoteAction.REPLACE.value)
        self.assertTrue(outcome.cancelled)  # 双边都进入 replace ⇒ 两笔撤单
        self.assertEqual(outcome.telemetry.cancel_count, len(outcome.cancelled))
        # 旧单已终态；补挂只能发生在**终态之后**（同轮内 cancel 已确认 ⇒ 允许补挂）
        self.assertLessEqual(len(stack.engine.manager.active_orders), 2)

    def test_replace_does_not_place_while_cancel_unconfirmed(self) -> None:
        from tests.execution_support_live import echoing_ack

        stack = OrchestrationStack.build(
            responses={"DELETE": lambda url: (_ for _ in ()).throw(TransportError("cancel response lost"))}
        )
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )
        posts_before = len([call for call in stack.transport.calls if call[0] == "POST"])

        stack.orchestrator.run_loop(
            state=market_state(best_bid=60_500.0, best_ask=60_510.0), prediction=record
        )

        posts_after = len([call for call in stack.transport.calls if call[0] == "POST"])
        self.assertEqual(posts_after, posts_before)  # 未确认终态 ⇒ 绝不补挂


class MultiplicityTest(unittest.TestCase):
    """SC-5 / SC-30：同侧 >1 active ⇒ ORDER_MULTIPLICITY_VIOLATION（fail closed，不擅自取舍）。"""

    def test_two_active_orders_on_one_side_is_a_violation(self) -> None:
        from execution.types import Order
        from portfolio.types import Side
        from risk.types import OrderProposal

        stack = OrchestrationStack.build()
        tracker = stack.tracker
        for index in range(2):
            order = tracker.create(
                OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.001, price=60_000.0, post_only=True),
                timestamp=NOW,
            )
            tracker.on_event(
                __import__("execution.events", fromlist=["OrderAccepted"]).OrderAccepted(
                    client_order_id=order.client_order_id, timestamp=NOW, exchange_order_id=str(100 + index)
                )
            )
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        posts_before = list(stack.transport.calls)

        outcome = stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )

        self.assertTrue(any("ORDER_MULTIPLICITY_VIOLATION" in note for note in outcome.telemetry.notes))
        self.assertEqual(stack.orchestrator.multiplicity_violations, 1)
        self.assertEqual([call for call in stack.transport.calls if call[0] == "POST"], [])
        # 不擅自撤/忽略其中一个：两笔订单都仍在本地账本里，且编排进入 RECONCILING（禁止新增暴露）
        self.assertEqual(len(tracker.orders), 2)
        self.assertIs(stack.orchestrator.state, OrchestratorState.RECONCILING)


class AuthorityGateTest(unittest.TestCase):
    """SC-11 / SC-12：authority 失效 ⇒ 新增 submit 为 0（cancel 不受影响）。"""

    def test_expired_authority_blocks_place(self) -> None:
        from tests.orchestration_support import authority

        stack = OrchestrationStack.build(authority_context=context(authority=authority(ttl_ms=1)))
        stack.orchestrator.clock = lambda: NOW + 10_000
        record = make_record(now_ms=NOW, ttl_ms=600_000)

        outcome = stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )

        self.assertEqual(stack.transport.calls, [])
        self.assertTrue(any("authority_invalid" in note for note in outcome.telemetry.notes))
        self.assertEqual(outcome.telemetry.submit_count, 0)

    def test_not_ready_state_blocks_submit(self) -> None:
        stack = OrchestrationStack.build()
        stack.orchestrator.mark_not_ready()
        record = make_record(now_ms=NOW, ttl_ms=600_000)

        stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )

        self.assertEqual(stack.transport.calls, [])

    def test_unknown_exposure_blocks_new_risk(self) -> None:
        """SC-10：LOST/不确定暴露未收敛 ⇒ 禁止新增暴露。"""
        stack = OrchestrationStack.build()
        tracker = stack.tracker
        from portfolio.types import Side
        from risk.types import OrderProposal

        order = tracker.create(
            OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.001, price=60_000.0, post_only=True),
            timestamp=NOW,
        )
        tracker.mark_lost(order.client_order_id, timestamp=NOW, reason="ack timeout")
        record = make_record(now_ms=NOW, ttl_ms=600_000)

        stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )

        self.assertEqual(stack.transport.calls, [])


class ObserveOnlyTest(unittest.TestCase):
    """SC-23：observe-only 期间不产生写请求。"""

    def test_observe_only_never_writes(self) -> None:
        stack = OrchestrationStack.build(execution_enabled=False, allow_write=False)
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        state = market_state(best_bid=60_000.0, best_ask=60_010.0)

        for _ in range(3):
            outcome = stack.orchestrator.run_loop(state=state, prediction=record)

        self.assertEqual(stack.transport.calls, [])
        self.assertIs(stack.orchestrator.state, OrchestratorState.OBSERVE_ONLY)
        self.assertTrue(outcome.telemetry.execution_disabled)


class TelemetryTest(unittest.TestCase):
    """SC-32：telemetry 能解释每轮动作原因。"""

    def test_telemetry_records_the_round_facts(self) -> None:
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)

        outcome = stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )
        telemetry = outcome.telemetry

        self.assertEqual(telemetry.loop_ts, NOW)
        self.assertEqual(telemetry.prediction_id, record.request_id)
        self.assertEqual(telemetry.prediction_age_ms, 0)
        self.assertEqual(telemetry.authority_id, "auth-1")
        self.assertTrue(telemetry.authority_valid)
        self.assertEqual(telemetry.bid_action, QuoteAction.PLACE.value)
        self.assertFalse(telemetry.reconciliation_triggered)
        mapping = telemetry.to_mapping()
        self.assertEqual(mapping["submit_count"], 2)  # 双边
        self.assertEqual(stack.orchestrator.telemetry.totals["submit_count"], 2)

    def test_sink_keeps_bounded_history(self) -> None:
        from live import LoopTelemetrySink

        sink = LoopTelemetrySink(limit=2)
        stack = OrchestrationStack.build()
        stack.orchestrator.telemetry = sink
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        for _ in range(4):
            stack.orchestrator.run_loop(
                state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record,
                            )

        self.assertEqual(len(sink.records), 2)
        self.assertEqual(len(sink.window(count=5)), 2)


class StopTest(unittest.TestCase):
    """SC-21 / SC-22：graceful stop 撤净挂单；**绝不**自动市价平仓。"""

    def test_stop_cancels_orders_and_reports_position(self) -> None:
        stack = OrchestrationStack.build()
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )

        report = stack.orchestrator.stop(position_qty=0.002)

        self.assertEqual(report.remaining_active, ())
        self.assertTrue(report.position_remains)  # 残留持仓如实报告
        self.assertIn("POSITION_REMAINS", report.notes)
        self.assertIs(stack.orchestrator.state, OrchestratorState.STOPPED)

    def test_stop_does_not_flatten(self) -> None:
        stack = OrchestrationStack.build()
        posts_before = list(stack.transport.calls)

        report = stack.orchestrator.stop(position_qty=0.003)

        self.assertFalse(report.position_remains is None)
        self.assertEqual([call for call in stack.transport.calls], posts_before)  # 无市价平仓（也没有 POST）
        self.assertEqual(report.position_qty, 0.003)


class ReconciliationTriggerTest(unittest.TestCase):
    """SC-9：UNKNOWN submit/cancel 触发 reconciliation（并在收敛前禁止新增暴露）。"""

    def test_unknown_submit_triggers_reconciliation(self) -> None:
        stack = OrchestrationStack.build(
            responses={"POST": lambda url: (_ for _ in ()).throw(ExecutionOutcomeUnknown(status=503, detail="x"))}
        )
        record = make_record(now_ms=NOW, ttl_ms=600_000)
        stack.orchestrator.run_loop(
            state=market_state(best_bid=60_000.0, best_ask=60_010.0), prediction=record
        )
        self.assertGreaterEqual(stack.adapter.unknown_submit_count, 1)  # 双边都未知

        outcome = stack.orchestrator.reconcile_now(reason="submit_unknown")

        self.assertEqual(outcome.triggered_by, "submit_unknown")
        self.assertIsNotNone(stack.orchestrator.last_reconciliation)

    def test_reconciliation_without_facts_is_not_converged(self) -> None:
        from connectors.binance.execution import ExternalFactsUnavailableError

        stack = OrchestrationStack.build(
            facts=type("F", (), {
                "parse_open_orders": lambda self, symbol: (_ for _ in ()).throw(
                    ExternalFactsUnavailableError("read failed")
                ),
                "parse_recent_fills": lambda self, symbol, since_ms=None: (),
            })(),
        )

        outcome = stack.orchestrator.reconcile_now(reason="test")

        self.assertFalse(outcome.converged)  # UNKNOWN ≠ EMPTY
        self.assertIs(stack.orchestrator.state, OrchestratorState.RECONCILING)


if __name__ == "__main__":
    unittest.main()
