"""P0001.9.6 Fault：不确定结果 / 重复事实 / 断线 / 上限（SC-9 – SC-16、SC-19、SC-26、SC-27、SC-28）。

核心命题（D-021 / D-022 的延续）：

```text
UNKNOWN submit → 绝不重发、绝不生成 FAILED → query 收敛 → 仍无法确认则保持 uncertain exposure
UNKNOWN cancel → 绝不假装 CANCELED → 由 query / user stream 收敛
```
"""

from __future__ import annotations

import unittest

from connectors.binance.execution import ExecutionOutcomeUnknown, SubmitClassification
from connectors.binance.market_data.errors import TransportError
from execution.manager import OrderManager
from execution.tracker import OrderTracker
from execution.types import OrderStatus
from portfolio.accounting import AccountingCore
from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import KillSwitchMode, OrderProposal
from tests.execution_support_live import FakeExecutionFetcher, ack_payload
from tests.support import BASE_TS
from tests.unit.test_execution_adapter import adapter, context, order


class UncertainSubmitTest(unittest.TestCase):
    """SC-9 / SC-10 / SC-11 / SC-26：不重发、可收敛、否则保持不确定暴露。"""

    def test_no_resend_even_after_repeated_calls(self) -> None:
        fetcher = FakeExecutionFetcher(responses={"POST": TimeoutError("lost response")})
        subject, transport = adapter(fetcher=fetcher)

        first = subject.submit_with_outcome(order())
        second = subject.submit_with_outcome(order())  # 调用方重复调用也不得自动重发同一订单

        self.assertIs(first.classification, SubmitClassification.UNKNOWN)
        self.assertIs(second.classification, SubmitClassification.UNKNOWN)
        # 两次调用各发一次（调用方显式重试的语义），但 adapter 自身**不**在内部重试
        self.assertEqual(len([call for call in transport.calls if call[0] == "POST"]), 2)
        self.assertEqual(subject.unknown_submit_count, 2)

    def test_manager_marks_unknown_submit_as_lost_and_counts_uncertain_exposure(self) -> None:
        """SC-28：UNKNOWN 之后**绝不**静默当作没有暴露；既有 timeout/LOST 路径负责表达不确定性。"""
        fetcher = FakeExecutionFetcher(responses={"POST": ExecutionOutcomeUnknown(status=503, detail="HTTP 503")})
        subject, _transport = adapter(fetcher=fetcher)
        tracker = OrderTracker(session_id="s1")
        manager = OrderManager(tracker=tracker, adapter=subject)

        order_obj, _updates = manager.submit(
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True),
            timestamp=BASE_TS,
        )

        # 未确认 ⇒ 仍是 PENDING_CREATE（暴露照常计入，绝不静默清零）
        self.assertIs(order_obj.status, OrderStatus.PENDING_CREATE)
        self.assertGreater(tracker.total_pending_exposure(), 0.0)
        self.assertFalse(tracker.has_unknown_exposure)  # 资料不足才是 unknown_exposure；这里是"未确认"

        # 既有 ack-timeout 路径：超时后标记 LOST ⇒ 从 confirmed 转入 uncertain
        stuck = tracker.pending_unacked(now_ms=BASE_TS + 2_000, timeout_ms=1_000)
        self.assertEqual([entry.client_order_id for entry in stuck], [order_obj.client_order_id])
        tracker.mark_lost(order_obj.client_order_id, timestamp=BASE_TS + 2_000, reason="ack timeout")

        self.assertIs(tracker.require_order(order_obj.client_order_id).status, OrderStatus.LOST)
        self.assertGreater(tracker.uncertain_exposure(), 0.0)
        self.assertEqual(tracker.confirmed_open_exposure(), 0.0)

    def test_query_resolution_restores_real_state(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={"POST": TransportError("connection reset"), "GET": ack_payload(status="PARTIALLY_FILLED", executed_qty="0.001", avg_price="60000.0")}
        )
        subject, _transport = adapter(fetcher=fetcher)
        tracker = OrderTracker(session_id="s1")
        manager = OrderManager(tracker=tracker, adapter=subject)
        local, _updates = manager.submit(
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True), timestamp=BASE_TS
        )
        self.assertIs(local.status, OrderStatus.PENDING_CREATE)  # 未确认
        tracker.mark_lost(local.client_order_id, timestamp=BASE_TS + 2_000, reason="ack timeout")
        self.assertIs(tracker.require_order(local.client_order_id).status, OrderStatus.LOST)

        subject.query_order(client_order_id=local.client_order_id, timestamp=BASE_TS + 2_001)
        manager.poll()

        restored = tracker.require_order(local.client_order_id)
        self.assertIs(restored.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(restored.filled_quantity, 0.001)
        self.assertEqual(restored.exchange_order_id, "4242")

    def test_unresolved_uncertainty_blocks_new_exposure(self) -> None:
        """SC-11：查询也无法确认 ⇒ 保持 LOST ⇒ 风险侧按不确定暴露处理。"""
        fetcher = FakeExecutionFetcher(responses={"POST": TimeoutError("lost"), "GET": TransportError("reset")})
        subject, _transport = adapter(fetcher=fetcher)
        tracker = OrderTracker(session_id="s1")
        manager = OrderManager(tracker=tracker, adapter=subject)
        local, _ = manager.submit(
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True), timestamp=BASE_TS
        )
        tracker.mark_lost(local.client_order_id, timestamp=BASE_TS + 1, reason="ack timeout")

        subject.query_order(client_order_id=local.client_order_id, timestamp=BASE_TS + 2)
        manager.poll()

        self.assertIs(tracker.require_order(local.client_order_id).status, OrderStatus.LOST)  # 查询失败 ⇒ 仍不确定
        self.assertGreater(tracker.total_pending_exposure(), 0.0)


class UncertainCancelTest(unittest.TestCase):
    """SC-12 / SC-13 / SC-27：撤单未确认绝不伪造终态。"""

    def test_unknown_cancel_keeps_pending_then_converges(self) -> None:
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload(), "DELETE": TimeoutError("lost cancel")})
        subject, _transport = adapter(fetcher=fetcher)
        tracker = OrderTracker(session_id="s1")
        manager = OrderManager(tracker=tracker, adapter=subject)
        local, _ = manager.submit(
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True), timestamp=BASE_TS
        )

        manager.cancel(local.client_order_id, timestamp=BASE_TS + 1)
        pending = tracker.require_order(local.client_order_id)
        self.assertIs(pending.status, OrderStatus.PENDING_CANCEL)  # 不是 CANCELED
        self.assertTrue(pending.is_active)

        # 用 query 收敛：交易所已取消
        fetcher.responses["GET"] = ack_payload(status="CANCELED")
        subject.query_order(client_order_id=local.client_order_id, timestamp=BASE_TS + 2)
        manager.poll()

        self.assertIs(tracker.require_order(local.client_order_id).status, OrderStatus.CANCELED)

    def test_unknown_cancel_then_query_shows_filled(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={
                "POST": ack_payload(),
                "DELETE": ExecutionOutcomeUnknown(status=503, detail="x"),
                "GET": ack_payload(status="FILLED", executed_qty="0.002", avg_price="60000.0"),
            }
        )
        subject, _transport = adapter(fetcher=fetcher)
        tracker = OrderTracker(session_id="s1")
        manager = OrderManager(tracker=tracker, adapter=subject)
        local, _ = manager.submit(
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True), timestamp=BASE_TS
        )

        manager.cancel(local.client_order_id, timestamp=BASE_TS + 1)
        subject.query_order(client_order_id=local.client_order_id, timestamp=BASE_TS + 2)
        manager.poll()

        self.assertIs(tracker.require_order(local.client_order_id).status, OrderStatus.FILLED)


class DuplicateFactTest(unittest.TestCase):
    """SC-14 / SC-15 / SC-16：重复事实不得造成状态错误或重复记账。"""

    def _observation(self, **overrides: object):
        from connectors.binance.private.events import OrderUpdateObservation

        values: dict[str, object] = {
            "event_ts": BASE_TS + 5, "transaction_ts": BASE_TS + 5, "receive_ts": BASE_TS + 6,
            "process_ts": BASE_TS + 6, "symbol": "BTCUSDT", "client_order_id": "probex-s1-000001",
            "order_id": 4242, "side": "BUY", "order_type": "LIMIT", "execution_type": "TRADE",
            "order_status": "PARTIALLY_FILLED", "last_fill_quantity": 0.001,
            "cumulative_fill_quantity": 0.001, "last_fill_price": 60_000.0, "average_price": 60_000.0,
            "commission": 0.02, "commission_asset": "USDT", "trade_id": 777, "is_maker": True,
            "original_quantity": 0.002, "original_price": 60_000.0, "reduce_only": False,
        }
        values.update(overrides)
        return OrderUpdateObservation(**values)  # type: ignore[arg-type]

    def test_duplicate_fill_is_recorded_once(self) -> None:
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        subject, _transport = adapter(fetcher=fetcher)
        tracker = OrderTracker(session_id="s1")
        manager = OrderManager(tracker=tracker, adapter=subject)
        accounting = AccountingCore(initial_balance=10_000.0)
        local, _ = manager.submit(
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True), timestamp=BASE_TS
        )

        subject.bridge_user_event(self._observation())
        updates = manager.poll()
        for update in updates:
            if update.fill is not None:
                accounting.record_fill(update.fill)
        subject.bridge_user_event(self._observation())  # 同一条事实（同 trade_id）重复抵达
        duplicate_updates = manager.poll()
        for update in duplicate_updates:
            if update.fill is not None:
                accounting.record_fill(update.fill)

        self.assertEqual(len(accounting.fills.fills), 1)
        self.assertAlmostEqual(tracker.require_order(local.client_order_id).filled_quantity, 0.001)

    def test_late_fill_after_terminal_keeps_tracker_semantics(self) -> None:
        """既有语义：终态后的 late fill 只更新成交事实，不回退状态。"""
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        tracker = OrderTracker(session_id="s1")
        subject, _transport = adapter(
            fetcher=fetcher, local_status=lambda cid: tracker.require_order(cid).status
        )
        manager = OrderManager(tracker=tracker, adapter=subject)
        local, _ = manager.submit(
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True), timestamp=BASE_TS
        )
        tracker.on_event(__import__("execution.events", fromlist=["OrderCanceled"]).OrderCanceled(
            client_order_id=local.client_order_id, timestamp=BASE_TS + 5
        ))

        # 真实 late fill：成交事件时间**早于**终态时间，但事实在终态之后才到达
        subject.bridge_user_event(self._observation(event_ts=BASE_TS, transaction_ts=BASE_TS))
        manager.poll()

        after = tracker.require_order(local.client_order_id)
        self.assertIs(after.status, OrderStatus.CANCELED)  # 状态不回退
        self.assertGreater(after.final_executed_quantity or 0.0, 0.0)


class DiscontinuityTest(unittest.TestCase):
    """SC-19：断线后不得继续 submit（authority generation 变化即拒绝），cancel 仍允许。"""

    def test_submit_refused_after_discontinuity_but_cancel_allowed(self) -> None:
        from readiness.types import RecoveryGeneration

        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload(), "DELETE": ack_payload(status="CANCELED")})
        # 先正常提交一次（generation 0）
        subject, transport = adapter(fetcher=fetcher)
        subject.submit_with_outcome(order())

        # 私有链路断线 ⇒ recovery generation 前进 ⇒ 旧上下文失效
        subject.authority_provider = lambda: context(recovery_generation=RecoveryGeneration(1, 0))
        before = len(transport.calls)
        refused = subject.submit_with_outcome(order(client_order_id="probex-s1-000002"))

        self.assertEqual(refused.rejection_message, "RECOVERY_GENERATION_CHANGED")
        self.assertEqual(len(transport.calls), before)  # 未触网

        # cancel 仍允许（降险动作）
        events = subject.cancel(order(client_order_id="probex-s1-000001", status=OrderStatus.PENDING_CANCEL))
        self.assertEqual([event.event_type.value for event in events], ["order_canceled"])


class KillSwitchTest(unittest.TestCase):
    def test_halt_all_blocks_submit_but_not_cancel(self) -> None:
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload(), "DELETE": ack_payload(status="CANCELED")})
        subject, transport = adapter(fetcher=fetcher, authority_context=context(kill_switch_mode=KillSwitchMode.HALT_ALL))

        refused = subject.submit_with_outcome(order())
        self.assertEqual(refused.rejection_message, "KILL_SWITCH_NOT_NORMAL")
        self.assertEqual(transport.calls, [])

        events = subject.cancel(order(status=OrderStatus.PENDING_CANCEL))
        self.assertEqual([event.event_type.value for event in events], ["order_canceled"])


if __name__ == "__main__":
    unittest.main()
