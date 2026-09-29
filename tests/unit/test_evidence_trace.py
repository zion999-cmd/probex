"""F-08 完整因果链：真实边界的风险判定 / 归一化证据 / ack 延迟 / reconciliation。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from decimal import ROUND_DOWN, Decimal

from execution.normalization import BoundedNormalizationEvidenceLog, OrderNormalizationError, \
    OrderNormalizer
from execution_safety.latency import BoundedLatencyLog
from product.service import ProductService
from product.types import Fact, RuntimeIdentity, RuntimeMode
from portfolio.types import Side
from risk.types import OrderProposal
from runtime.latency_observer import ExecutionLatencyObserver
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS, SYMBOL


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(mode=RuntimeMode.PAPER, environment="local", venue="binance",
                           symbol=SYMBOL, runtime_id="rt-f08", started_at=BASE_TS,
                           data_timestamp=Fact.unknown("no data"))


def _normalizer() -> OrderNormalizer:
    return OrderNormalizer(tick_size=Decimal("0.1"), step_size=Decimal("0.0001"),
                           price_rounding=ROUND_DOWN, quantity_rounding=ROUND_DOWN)


class EngineEvidenceTest(unittest.TestCase):
    def test_allow_and_reject_are_both_recorded(self) -> None:
        stack = ExecutionStack.build()
        allowed = stack.engine.submit(OrderProposal(symbol=SYMBOL, side=Side.BUY,
                                                    quantity=1.0, price=100.0), now_ms=BASE_TS)
        self.assertTrue(allowed.submitted)
        rejected = stack.engine.submit(OrderProposal(
            symbol=SYMBOL, side=Side.BUY,
            quantity=1.0, price=-1.0), now_ms=BASE_TS + 1)

        log = stack.engine.decision_log
        self.assertEqual(len(log), 2)
        self.assertIsNone(log[0].decision.reason_code)             # allow
        self.assertEqual(log[0].client_order_id, allowed.order.client_order_id)
        self.assertIsNotNone(log[1].decision.reason_code)          # reject
        self.assertIsNone(log[1].client_order_id)
        self.assertFalse(rejected.submitted)

    def test_decision_log_is_bounded(self) -> None:
        stack = ExecutionStack.build()
        stack.engine.decision_capacity = 2
        stack.engine.__post_init__()
        side = Side.BUY
        for _ in range(3):
            stack.engine.submit(OrderProposal(symbol=SYMBOL, side=side, quantity=1.0, price=100.0),
                                now_ms=BASE_TS)
        self.assertEqual(len(stack.engine.decision_log), 2)


class NormalizationEvidenceTest(unittest.TestCase):
    def test_evidence_is_produced_and_bound_to_the_order(self) -> None:
        stack = ExecutionStack.build()
        stack.engine.normalizer = _normalizer()
        result = stack.engine.submit(OrderProposal(
            symbol=SYMBOL, side=Side.BUY,
            quantity=1.00007, price=100.05), now_ms=BASE_TS)

        evidence = stack.engine.normalization_log.evidence()
        self.assertEqual(len(evidence), 1)
        self.assertFalse(evidence[0].rejected)
        self.assertTrue(evidence[0].adjusted)
        self.assertEqual(evidence[0].normalized_price, "100")
        self.assertEqual(evidence[0].normalized_quantity, "1")
        self.assertEqual(evidence[0].client_order_id, result.order.client_order_id)
        self.assertEqual(evidence[0].price_rounding, ROUND_DOWN)

    def test_rejected_normalization_is_evidence_not_a_silent_fix(self) -> None:
        stack = ExecutionStack.build()
        stack.engine.normalizer = _normalizer()
        with self.assertRaises(OrderNormalizationError):
            stack.engine.submit(OrderProposal(
                symbol=SYMBOL, side=Side.BUY,
                quantity=1.0, price=0.01), now_ms=BASE_TS)   # 0.01 量化到 0.1 => 0 => reject
        evidence = stack.engine.normalization_log.evidence()
        self.assertEqual(len(evidence), 1)
        self.assertTrue(evidence[0].rejected)
        self.assertTrue(evidence[0].reject_reason)


class TraceCompositionTest(unittest.TestCase):
    def _service(self, **overrides: object) -> ProductService:
        stack = ExecutionStack.build()
        stack.engine.normalizer = _normalizer()
        log = BoundedLatencyLog(capacity=100)
        observer = ExecutionLatencyObserver(log=log, clock=lambda: BASE_TS)
        # engine 的 latency_observer 是 callable（assembly 用 lambda 包 observer）
        stack.engine.latency_observer = lambda kind, ts: observer.note(kind, ts)

        stack.engine.submit(OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=1.00007, price=100.05),
                            now_ms=BASE_TS)
        order = stack.engine.decision_log[-1].client_order_id
        stack.engine.cancel(order, now_ms=BASE_TS)

        def lifecycle() -> tuple[object, ...]:
            return tuple(SimpleNamespace(client_order_id=item.client_order_id,
                                         timestamp=item.updated_at,
                                         event_name=f"OrderStatus:{item.status.value}",
                                         reason="")
                         for item in stack.tracker.orders
                         if item.status.is_terminal or item.status.is_lost)

        values: dict[str, object] = {
            "identity": _identity(),
            "tracker": lambda: stack.tracker,
            "risk_decisions": lambda: stack.engine.decision_log,
            "normalization_evidence": lambda: stack.engine.normalization_log.evidence(),
            "ack_latency": lambda: log,
            "execution_events": lifecycle,
        }
        values.update(overrides)
        return ProductService(**values)  # type: ignore[arg-type]

    def test_trace_contains_real_risk_normalization_ack_and_order(self) -> None:
        trace = self._service().snapshot().evidence.trace
        stages = [entry.stage for entry in trace]
        for stage in ("market_state", "risk", "normalization", "order", "ack", "execution_event",
                      "cancel"):
            with self.subTest(stage=stage):
                self.assertIn(stage, stages)
        risk = next(entry for entry in trace if entry.stage == "risk")
        self.assertEqual(risk.outcome, "allow")
        self.assertEqual(risk.identity_kind, "client_order_id")
        normalization = next(entry for entry in trace if entry.stage == "normalization")
        self.assertIn("100.05 -> 100", normalization.detail)
        ack = [entry for entry in trace if entry.stage == "ack"]
        self.assertTrue(any(entry.latency_ms.known for entry in ack))

    def test_trace_is_time_ordered(self) -> None:
        trace = self._service().snapshot().evidence.trace
        ordered = [int(entry.ts.value) for entry in trace if entry.ts.known]
        self.assertEqual(ordered, sorted(ordered))

    def test_absent_stages_are_explicit_with_reasons(self) -> None:
        trace = self._service().snapshot().evidence.trace
        by_stage = {entry.stage: entry for entry in trace}
        for stage in ("prediction", "maker_decision", "readiness"):
            with self.subTest(stage=stage):
                self.assertEqual(by_stage[stage].outcome, "absent")
                self.assertFalse(by_stage[stage].reason_code.known)
        # 未接线的归一化阶段：必须显式 ABSENT（不是被静默跳过）
        without = ProductService(identity=_identity()).snapshot().evidence.trace
        self.assertIn("normalization", [entry.stage for entry in without])

    def test_reconciliation_enters_the_same_trace(self) -> None:
        events = (SimpleNamespace(ts=BASE_TS + 5, identity="reconciliation:1",
                                                      identity_kind="reconciliation_id",
                                                      outcome="requested",
                                                      reason_code="RECONCILIATION_REQUIRED",
                                                      detail="controlled entry"),)
        trace = self._service(reconciliation_events=lambda: events).snapshot().evidence.trace
        reconciliation = [entry for entry in trace if entry.stage == "reconciliation"]
        self.assertEqual(len(reconciliation), 1)
        self.assertEqual(reconciliation[0].reason_code.value, "RECONCILIATION_REQUIRED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
