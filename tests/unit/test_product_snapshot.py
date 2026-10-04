"""P0001.10 产品读模型单测（SC-1 / SC-2 / SC-3 / SC-6 / SC-7 / SC-8）。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from product.service import ProductService
from product.types import (
    SCHEMA_VERSION,
    RuntimeIdentity,
    RuntimeMode,
)


def identity(mode: RuntimeMode = RuntimeMode.PAPER) -> RuntimeIdentity:
    return RuntimeIdentity(mode=mode, environment="testnet", venue="binance", symbol="BTCUSDT",
                           runtime_id="rt-1", started_at=1_000, data_timestamp=_fact(2_000))


def _fact(value: object):
    from product.types import Fact

    return Fact.of(value)


def market_state(**overrides: object) -> object:
    values: dict[str, object] = {
        "identity": SimpleNamespace(state_hash="ms-1"),
        "quality": SimpleNamespace(healthy=True, tradeable=True, window_coverage_ms=305_000),
        "price": SimpleNamespace(best_bid=60_000.0, best_ask=60_001.0, spread_bps=1.7),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def prediction_record() -> object:
    return SimpleNamespace(request_id="pred-1", sequence=7, provider="systemone", model="jev",
                           as_of=2_000, expires_at=5_000, latency_ms=677, market_state_hash="ms-1",
                           prediction=SimpleNamespace(derived_confidence=0.51))


def maker_decision() -> object:
    return SimpleNamespace(
        at_ms=2_100, mode="both", detail="quoting both sides",
        blocked_by=None,
        bid=SimpleNamespace(action="place", client_order_id="probex-s1-000001",
                            price=82_890.0, quantity=0.0007),
        ask=SimpleNamespace(action="none", client_order_id=None, price=None,
                            quantity=None),
    )


def order(client_order_id: str = "probex-s1-000001", *,
          correlation: object | None = SimpleNamespace(
              decision_id="d-abc-2100", instrument_id="paper:BTCUSDT", venue_id="paper",
              prediction_id="pred-1", market_state_hash="sha256:deadbeef")) -> object:
    """订单事实（P0001.15 §15：identity / decision 关联来自订单自身的 canonical correlation）。"""
    return SimpleNamespace(client_order_id=client_order_id, side="buy",
                           status="OPEN", price=82_890.0,
                           quantity=0.0007, filled_quantity=0.0, reduce_only=False, created_at=2_200,
                           updated_at=2_200, correlation=correlation)


def tracker(orders: tuple[object, ...] = ()) -> object:
    class Tracker:
        has_unknown_exposure = False

        def active(self) -> tuple[object, ...]:
            return orders

        def uncertain_exposure(self) -> float:
            return 0.0

        def total_pending_exposure(self) -> float:
            return sum(float(o.price) * float(o.quantity) for o in orders)

    return Tracker()


def service(**overrides: object) -> ProductService:
    values: dict[str, object] = {
        "identity": identity(),
        "market_state": lambda: market_state(),
        "prediction": lambda: prediction_record(),
        "maker_decision": lambda: maker_decision(),
        "risk_snapshot": lambda: SimpleNamespace(kill_switch_mode="NORMAL",
                                                 realized_pnl_today=-1.27395553, drawdown=None,
                                                 peak_equity=None),
        "risk_limits": lambda: SimpleNamespace(max_position_qty=0.003, max_position_notional=100.0,
                                               max_open_order_exposure=100.0, max_daily_loss=50.0,
                                               max_drawdown_pct=0.10),
        "tracker": lambda: tracker((order(),)),
        "accounting": lambda: SimpleNamespace(
            balance=10_000.0, equity=9_998.73,
            position=lambda symbol: SimpleNamespace(qty=0.0, average_entry_price=None, mark_price=60_000.0,
                                                    unrealized_pnl=0.0, realized_pnl=-1.27395553,
                                                    fees_paid=0.2, funding_paid=0.0)),
        "readiness": lambda: SimpleNamespace(status="blocked",
                                             scope=None,
                                             reasons=("PRIVATE_LATENCY_UNOBSERVED",),
                                             details=("no private business events observed yet",)),
        "authority_id": lambda: None,
        "health": lambda: {"market_healthy": True, "private_stream_state": "ACTIVE", "uptime_ms": 600_000},
        "risk_rejects": lambda: ("MAX_POSITION_EXCEEDED",),
        "execution_events": tuple,
        "clock": lambda: 9_999,
        "prediction_fresh": lambda: True,
    }
    values.update(overrides)
    return ProductService(**values)  # type: ignore[arg-type]


class SnapshotCompositionTest(unittest.TestCase):
    def test_snapshot_has_versioned_schema_and_runtime_identity(self) -> None:
        snap = service().snapshot()
        self.assertEqual(snap.schema_version, SCHEMA_VERSION)
        self.assertIs(snap.runtime.mode, RuntimeMode.PAPER)
        self.assertEqual(snap.runtime.symbol, "BTCUSDT")
        self.assertEqual(snap.generated_at, 9_999)

    def test_facts_are_passed_through_not_recomputed(self) -> None:
        """SC-4：产品层只搬运 Owner 给的值，不重算。"""
        snap = service().snapshot()
        self.assertEqual(snap.market.best_bid.value, 60_000.0)
        self.assertEqual(snap.market.window_coverage_ms.value, 305_000)
        self.assertEqual(snap.portfolio.realized_pnl.value, -1.27395553)
        self.assertEqual(snap.risk.max_position_qty.value, 0.003)

    def test_unknown_is_preserved_and_never_zero(self) -> None:
        """SC-3：缺失事实必须是 EXPLICIT UNKNOWN，而不是 0 / 空 / healthy。"""
        empty = service(
            market_state=lambda: None,
            prediction=lambda: None,
            maker_decision=lambda: None,
            risk_snapshot=lambda: None,
            tracker=lambda: None,
            accounting=lambda: None,
            readiness=lambda: None,
            prediction_fresh=lambda: None,
        ).snapshot()

        for fact, label in (
            (empty.market.best_bid, "market.best_bid"),
            (empty.prediction.request_id, "prediction.request_id"),
            (empty.strategy.bid_action, "strategy.bid_action"),
            (empty.risk.kill_switch_mode, "risk.kill_switch_mode"),
            (empty.execution.uncertain_exposure, "execution.uncertain_exposure"),
            (empty.portfolio.position_qty, "portfolio.position_qty"),
            (empty.portfolio.equity, "portfolio.equity"),
            (empty.readiness.status, "readiness.status"),
            (empty.readiness.authority_id, "readiness.authority_id"),
        ):
            with self.subTest(field=label):
                self.assertFalse(fact.known)
                self.assertIsNone(fact.value)
                self.assertTrue(fact.reason)
        # drawdown 未知绝不能变成 0（P0001.9.4 事实链）
        self.assertFalse(empty.risk.drawdown.known)
        self.assertIsNone(empty.risk.drawdown.value)

    def test_risk_reject_reason_codes_are_exposed(self) -> None:
        """SC-5：每个 Risk reject 都能看到正式 reason code。"""
        snap = service().snapshot()
        self.assertEqual(snap.risk.rejects, ("MAX_POSITION_EXCEEDED",))
        self.assertEqual(snap.evidence.risk_rejects, ("MAX_POSITION_EXCEEDED",))

    def test_every_mode_shares_one_schema(self) -> None:
        """SC-2：REPLAY / PAPER / TESTNET 共用同一 snapshot schema。"""
        shapes = []
        for mode in (RuntimeMode.REPLAY, RuntimeMode.PAPER, RuntimeMode.TESTNET, RuntimeMode.LIVE):
            snap = service(identity=identity(mode)).snapshot()
            shapes.append(snap.schema_version)
            self.assertIs(snap.runtime.mode, mode)
        self.assertEqual(set(shapes), {SCHEMA_VERSION})


class EvidenceTraceTest(unittest.TestCase):
    def test_maker_decision_exposes_input_prediction_identity(self) -> None:
        """SC-6。"""
        snap = service().snapshot()
        decision_entry = next(e for e in snap.evidence.trace if e.stage == "maker_decision")
        self.assertEqual(decision_entry.identity.value, "pred-1")

    def test_active_order_traces_back_to_decision(self) -> None:
        """SC-7 / P0001.15 §15：每个 active order 都能追溯到产生它的 decision（含 instrument/venue）。"""
        snap = service().snapshot()
        order_view = snap.execution.active_orders[0]
        self.assertEqual(order_view.client_order_id, "probex-s1-000001")
        self.assertEqual(order_view.decision_id.value, "d-abc-2100")
        self.assertEqual(order_view.instrument_id.value, "paper:BTCUSDT")
        self.assertEqual(order_view.venue_id.value, "paper")
        self.assertEqual(order_view.prediction_id.value, "pred-1")

    def test_order_without_correlation_is_explicitly_unknown(self) -> None:
        """没有 canonical correlation ⇒ 如实 UNKNOWN（不靠时间/名称模糊匹配）。"""
        snap = service(tracker=lambda: tracker((order("probex-s1-999999", correlation=None),))).snapshot()
        view = snap.execution.active_orders[0]
        for fact in (view.decision_id, view.instrument_id, view.venue_id, view.prediction_id):
            with self.subTest(field=fact):
                self.assertFalse(fact.known)
                self.assertIsNone(fact.value)
                self.assertTrue(fact.reason)

    def test_readiness_blockers_are_listed_verbatim(self) -> None:
        """SC-8。"""
        snap = service().snapshot()
        self.assertEqual(snap.evidence.readiness_blockers, ("PRIVATE_LATENCY_UNOBSERVED",))
        self.assertEqual(snap.readiness.reasons, ("PRIVATE_LATENCY_UNOBSERVED",))
        readiness_entry = next(e for e in snap.evidence.trace if e.stage == "readiness")
        self.assertEqual(readiness_entry.reason_code.value, "PRIVATE_LATENCY_UNOBSERVED")
        self.assertEqual(readiness_entry.outcome, "blocked")

    def test_trace_is_structured_and_ordered(self) -> None:
        snap = service().snapshot()
        stages = [entry.stage for entry in snap.evidence.trace]
        self.assertEqual(stages[:3], ["market_state", "prediction", "maker_decision"])
        self.assertIn("order", stages)
