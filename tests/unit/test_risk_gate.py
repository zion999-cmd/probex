"""RiskGate：暴露分类、reduce-only 语义、各项限额与 fail closed（SC-7 – SC-10）。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from portfolio.accounting import AccountingCore
from portfolio.types import InvalidLiquidationInfoError, LiquidationInfo, Side
from risk.gate import RiskGate, classify_exposure
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import (
    ExposureClass,
    InvalidOrderProposalError,
    OrderProposal,
    RiskDecisionType,
    RiskReasonCode,
    RiskSnapshot,
)
from tests.support import BASE_TS, SYMBOL, make_fill, make_funding


def _proposal(
    side: Side = Side.BUY,
    quantity: float = 0.1,
    price: float = 100.0,
    *,
    reduce_only: bool = False,
    post_only: bool = False,
    symbol: str = SYMBOL,
) -> OrderProposal:
    return OrderProposal(
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        reduce_only=reduce_only,
        post_only=post_only,
    )


def _snapshot(
    *,
    position_qty: float = 0.0,
    entry_price: float = 100.0,
    mark: float | None = 100.0,
    balance: float = 10_000.0,
    fee: float = 0.0,
    now_ms: int = BASE_TS,
    mark_ts: int | None = None,
    open_order_exposure: float = 0.0,
    liquidation: LiquidationInfo | None = None,
    day_start_ts: int | None = None,
    funding: tuple[float, ...] = (),
) -> RiskSnapshot:
    core = AccountingCore(initial_balance=balance)
    if position_qty > 0:
        core.record_fill(make_fill("f1", Side.BUY, entry_price, position_qty, fee=fee))
    elif position_qty < 0:
        core.record_fill(make_fill("f1", Side.SELL, entry_price, abs(position_qty), fee=fee))
    if mark is not None:
        core.update_mark_price(SYMBOL, mark, timestamp=now_ms if mark_ts is None else mark_ts)
    for index, amount in enumerate(funding):
        core.record_funding(make_funding(amount, timestamp=now_ms + index))
    return build_risk_snapshot(
        core,
        symbol=SYMBOL,
        now_ms=now_ms,
        open_order_exposure=open_order_exposure,
        liquidation=liquidation,
        day_start_ts=day_start_ts,
    )


class ExposureClassTest(unittest.TestCase):
    def test_flat_position_is_increasing(self) -> None:
        self.assertIs(classify_exposure(_proposal(), _snapshot()), ExposureClass.INCREASING)

    def test_same_direction_is_increasing(self) -> None:
        long = _snapshot(position_qty=1.0)
        self.assertIs(classify_exposure(_proposal(Side.BUY, 0.5), long), ExposureClass.INCREASING)

    def test_partial_close_is_reducing(self) -> None:
        long = _snapshot(position_qty=1.0)
        self.assertIs(classify_exposure(_proposal(Side.SELL, 0.5), long), ExposureClass.REDUCING)

    def test_exact_close_is_reducing(self) -> None:
        long = _snapshot(position_qty=1.0)
        self.assertIs(classify_exposure(_proposal(Side.SELL, 1.0), long), ExposureClass.REDUCING)

    def test_reversal_is_reversing(self) -> None:
        long = _snapshot(position_qty=1.0)
        short = _snapshot(position_qty=-1.0)
        self.assertIs(classify_exposure(_proposal(Side.SELL, 2.0), long), ExposureClass.REVERSING)
        self.assertIs(classify_exposure(_proposal(Side.BUY, 2.0), short), ExposureClass.REVERSING)


class OrderProposalTest(unittest.TestCase):
    def test_signed_quantity_and_notional(self) -> None:
        buy = _proposal(Side.BUY, 2.0, 100.0)
        sell = _proposal(Side.SELL, 2.0, 100.0)
        self.assertEqual(buy.signed_quantity, 2.0)
        self.assertEqual(sell.signed_quantity, -2.0)
        self.assertEqual(buy.notional, 200.0)

    def test_type_validation(self) -> None:
        with self.assertRaises(InvalidOrderProposalError):
            _proposal(symbol="")
        with self.assertRaises(InvalidOrderProposalError):
            OrderProposal(symbol=SYMBOL, side="buy", quantity=1.0, price=1.0)  # type: ignore[arg-type]
        with self.assertRaises(InvalidOrderProposalError):
            OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=float("nan"), price=1.0)

    def test_non_positive_values_pass_construction_and_are_gate_checks(self) -> None:
        # §11：订单数量/价格非法属于 gate 的硬检查（reduce-only 也不能绕过）
        self.assertEqual(_proposal(quantity=0.0).quantity, 0.0)
        self.assertEqual(_proposal(price=-1.0).price, -1.0)


class RiskLimitsTest(unittest.TestCase):
    def test_defaults_are_disabled(self) -> None:
        limits = RiskLimits()
        self.assertEqual(limits.enabled_checks(), ("available_balance",))
        self.assertEqual(limits.effective_leverage, 1.0)

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            RiskLimits(max_position_qty=0.0)
        with self.assertRaises(ValueError):
            RiskLimits(max_daily_loss=-1.0)
        with self.assertRaises(ValueError):
            RiskLimits(max_drawdown_pct=1.5)
        with self.assertRaises(ValueError):
            RiskLimits(max_leverage=0.5)
        with self.assertRaises(ValueError):
            RiskLimits(max_mark_age_ms=0)
        with self.assertRaises(ValueError):
            RiskLimits(kill_switch="yes")  # type: ignore[arg-type]

    def test_enabled_checks_order(self) -> None:
        limits = RiskLimits(
            kill_switch=True,
            max_mark_age_ms=1_000,
            max_position_qty=1.0,
            max_position_notional=1.0,
            max_open_order_exposure=1.0,
            max_leverage=2.0,
            max_daily_loss=1.0,
            max_drawdown_pct=0.5,
            min_liquidation_distance_bps=100.0,
        )
        self.assertEqual(
            limits.enabled_checks(),
            (
                "kill_switch",
                "mark_age",
                "position_qty",
                "position_notional",
                "open_order_exposure",
                "available_balance",
                "leverage",
                "daily_loss",
                "drawdown",
                "liquidation_distance",
            ),
        )


class HardCheckTest(unittest.TestCase):
    """硬检查：reduce-only 也不能绕过（§11）。"""

    def setUp(self) -> None:
        self.snapshot = _snapshot(position_qty=1.0)
        self.gate = RiskGate(RiskLimits(max_position_qty=1.0, max_position_notional=100.0))

    def test_symbol_mismatch(self) -> None:
        decision = self.gate.evaluate(_proposal(symbol="ETHUSDT"), self.snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.SYMBOL_MISMATCH)

    def test_invalid_quantity_even_for_reduce_only(self) -> None:
        decision = self.gate.evaluate(_proposal(Side.SELL, 0.0, reduce_only=True), self.snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.INVALID_ORDER_QUANTITY)

    def test_invalid_price_even_for_reduce_only(self) -> None:
        decision = self.gate.evaluate(_proposal(Side.SELL, 0.5, 0.0, reduce_only=True), self.snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.INVALID_ORDER_PRICE)

    def test_book_unhealthy(self) -> None:
        decision = self.gate.evaluate(_proposal(), self.snapshot, book_healthy=False)
        self.assertIs(decision.reason_code, RiskReasonCode.BOOK_UNHEALTHY)
        reduce_only = self.gate.evaluate(_proposal(Side.SELL, 0.5, reduce_only=True), self.snapshot, book_healthy=False)
        self.assertIs(reduce_only.reason_code, RiskReasonCode.BOOK_UNHEALTHY)

    def test_missing_mark_price(self) -> None:
        snapshot = _snapshot(position_qty=1.0, mark=None)
        for reduce_only in (False, True):
            with self.subTest(reduce_only=reduce_only):
                decision = self.gate.evaluate(
                    _proposal(Side.SELL if reduce_only else Side.BUY, 0.5, reduce_only=reduce_only), snapshot
                )
                self.assertIs(decision.reason_code, RiskReasonCode.MISSING_MARK_PRICE)

    def test_stale_mark(self) -> None:
        gate = RiskGate(RiskLimits(max_mark_age_ms=10))
        stale = _snapshot(position_qty=1.0, mark=100.0, mark_ts=BASE_TS, now_ms=BASE_TS + 1_000)

        decision = gate.evaluate(_proposal(), stale)

        self.assertIs(decision.reason_code, RiskReasonCode.STALE_MARK)
        self.assertEqual(stale.mark_age_ms, 1_000)

        fresh = _snapshot(position_qty=1.0, mark=100.0, mark_ts=BASE_TS, now_ms=BASE_TS + 5)
        self.assertIs(
            RiskGate(RiskLimits(max_mark_age_ms=10)).evaluate(_proposal(), fresh).decision,
            RiskDecisionType.ALLOW,
        )

    def test_unknown_mark_age_with_configured_limit_fails_closed(self) -> None:
        # 显式构造「有 mark 价格但年龄未知」的快照（异常来源：adapter 未提供时间戳）
        snapshot = replace(_snapshot(position_qty=1.0, mark=100.0), mark_age_ms=None)
        self.assertIsNotNone(snapshot.mark_price)
        self.assertIsNone(snapshot.mark_age_ms)

        decision = RiskGate(RiskLimits(max_mark_age_ms=10)).evaluate(_proposal(), snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.STALE_MARK)

    def test_kill_switch_blocks_everything(self) -> None:
        gate = RiskGate(RiskLimits(kill_switch=True))
        for reduce_only in (False, True):
            with self.subTest(reduce_only=reduce_only):
                decision = gate.evaluate(
                    _proposal(Side.SELL if reduce_only else Side.BUY, 0.5, reduce_only=reduce_only), self.snapshot
                )
                self.assertIs(decision.reason_code, RiskReasonCode.KILL_SWITCH)

    def test_reduce_only_that_increases_exposure_is_rejected(self) -> None:
        flat = _snapshot()
        decision = self.gate.evaluate(_proposal(Side.BUY, 0.5, reduce_only=True), flat)
        self.assertIs(decision.reason_code, RiskReasonCode.REDUCE_ONLY_WOULD_INCREASE)

        reversal = self.gate.evaluate(_proposal(Side.SELL, 2.0, reduce_only=True), self.snapshot)
        self.assertIs(reversal.reason_code, RiskReasonCode.REDUCE_ONLY_WOULD_INCREASE)


class ReduceOnlyExemptionTest(unittest.TestCase):
    """SC-7：真正降低暴露的 reduce-only 订单不会因限额已满被误拒。"""

    def test_reduce_only_bypasses_every_increasing_limit(self) -> None:
        snapshot = _snapshot(
            position_qty=1.0,
            mark=100.0,
            balance=100.0,
            open_order_exposure=100.0,
            liquidation=LiquidationInfo.from_prices(symbol=SYMBOL, liquidation_price=99.9, mark_price=100.0),
            day_start_ts=utc_day_start_ms(BASE_TS),
            funding=(-500.0,),
        )
        gate = RiskGate(
            RiskLimits(
                max_position_qty=0.1,
                max_position_notional=10.0,
                max_open_order_exposure=10.0,
                max_leverage=1.0,
                max_daily_loss=1.0,
                max_drawdown_pct=0.01,
                min_liquidation_distance_bps=10_000.0,
            )
        )

        decision = gate.evaluate(_proposal(Side.SELL, 0.5, 100.0, reduce_only=True), snapshot)

        self.assertIs(decision.decision, RiskDecisionType.ALLOW)
        self.assertIsNone(decision.reason_code)
        self.assertIn("exposure_class:reducing", decision.checks_applied)
        self.assertNotIn("daily_loss", decision.checks_applied)

    def test_reduce_only_still_checked_for_order_parameters(self) -> None:
        snapshot = _snapshot(position_qty=1.0)
        gate = RiskGate(RiskLimits(max_position_qty=0.01))

        decision = gate.evaluate(_proposal(Side.SELL, 0.5, -1.0, reduce_only=True), snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.INVALID_ORDER_PRICE)


class IncreasingLimitsTest(unittest.TestCase):
    """SC-8：增加暴露超过任一限额必须 REJECT。"""

    def test_allow_within_all_limits(self) -> None:
        snapshot = _snapshot(position_qty=0.0, balance=1_000.0)
        gate = RiskGate(
            RiskLimits(max_position_qty=1.0, max_position_notional=1_000.0, max_open_order_exposure=1_000.0)
        )

        decision = gate.evaluate(_proposal(Side.BUY, 0.5, 100.0), snapshot)

        self.assertIs(decision.decision, RiskDecisionType.ALLOW)
        self.assertIsNone(decision.reason_code)
        self.assertIn("position_qty", decision.checks_applied)
        self.assertIn("available_balance", decision.checks_applied)

    def test_position_limit(self) -> None:
        snapshot = _snapshot(position_qty=0.5)
        gate = RiskGate(RiskLimits(max_position_qty=0.6))

        decision = gate.evaluate(_proposal(Side.BUY, 0.2), snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.POSITION_LIMIT)

    def test_notional_limit(self) -> None:
        snapshot = _snapshot(position_qty=0.0)
        gate = RiskGate(RiskLimits(max_position_notional=50.0))

        decision = gate.evaluate(_proposal(Side.BUY, 1.0, 100.0), snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.NOTIONAL_LIMIT)

    def test_open_order_exposure_limit(self) -> None:
        snapshot = _snapshot(position_qty=0.0, open_order_exposure=90.0)
        gate = RiskGate(RiskLimits(max_open_order_exposure=100.0))

        decision = gate.evaluate(_proposal(Side.BUY, 0.2, 100.0), snapshot)  # 90 + 20 > 100
        self.assertIs(decision.reason_code, RiskReasonCode.OPEN_ORDER_EXPOSURE_LIMIT)

    def test_available_balance(self) -> None:
        snapshot = _snapshot(position_qty=0.0, balance=100.0, open_order_exposure=80.0)
        gate = RiskGate(RiskLimits())

        decision = gate.evaluate(_proposal(Side.BUY, 0.5, 100.0), snapshot)  # 50 > 20
        self.assertIs(decision.reason_code, RiskReasonCode.INSUFFICIENT_AVAILABLE_BALANCE)

    def test_available_balance_honours_leverage(self) -> None:
        snapshot = _snapshot(position_qty=0.0, balance=100.0)
        gate = RiskGate(RiskLimits(max_leverage=5.0))

        decision = gate.evaluate(_proposal(Side.BUY, 4.0, 100.0), snapshot)  # 400 <= 100*5
        self.assertIs(decision.decision, RiskDecisionType.ALLOW)

        over = gate.evaluate(_proposal(Side.BUY, 6.0, 100.0), snapshot)
        self.assertIs(over.reason_code, RiskReasonCode.INSUFFICIENT_AVAILABLE_BALANCE)

    def test_leverage_limit(self) -> None:
        # 已有 long 10 @100（notional 1000，equity 1000），max_leverage=2：
        # 新订单 notional 1500 ≤ available capacity 2000（余额检查通过），
        # 但 projected notional (1000+1500)/1000 = 2.5 > 2 → LEVERAGE_LIMIT
        snapshot = _snapshot(position_qty=10.0, balance=1_000.0, mark=100.0)
        gate = RiskGate(RiskLimits(max_leverage=2.0))

        decision = gate.evaluate(_proposal(Side.BUY, 15.0, 100.0), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.LEVERAGE_LIMIT)
        self.assertIn("available_balance", decision.checks_applied)

    def test_reversal_is_treated_as_increasing(self) -> None:
        snapshot = _snapshot(position_qty=1.0)
        gate = RiskGate(RiskLimits(max_position_qty=1.5))

        decision = gate.evaluate(_proposal(Side.SELL, 2.0, 100.0), snapshot)  # projected -1

        self.assertIs(decision.decision, RiskDecisionType.ALLOW)
        self.assertIn("exposure_class:reversing", decision.checks_applied)

        tighter = RiskGate(RiskLimits(max_position_qty=0.5)).evaluate(_proposal(Side.SELL, 2.0, 100.0), snapshot)
        self.assertIs(tighter.reason_code, RiskReasonCode.POSITION_LIMIT)

    def test_first_violated_check_wins(self) -> None:
        snapshot = _snapshot(position_qty=0.9)
        gate = RiskGate(RiskLimits(max_position_qty=1.0, max_position_notional=1.0, max_open_order_exposure=1.0))

        decision = gate.evaluate(_proposal(Side.BUY, 0.5, 100.0), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.POSITION_LIMIT)
        self.assertNotIn("notional", decision.details)


class DailyLossAndDrawdownTest(unittest.TestCase):
    """SC-9：daily loss / drawdown / kill switch 命中时关闭新增暴露。"""

    def test_daily_loss_limit(self) -> None:
        snapshot = _snapshot(
            position_qty=0.0, balance=1_000.0, day_start_ts=utc_day_start_ms(BASE_TS), funding=(-60.0,)
        )
        gate = RiskGate(RiskLimits(max_daily_loss=50.0))

        decision = gate.evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.DAILY_LOSS_LIMIT)
        self.assertIn("daily_loss", decision.checks_applied)

    def test_daily_loss_boundary_is_inclusive(self) -> None:
        snapshot = _snapshot(position_qty=0.0, balance=1_000.0, day_start_ts=utc_day_start_ms(BASE_TS), funding=(-50.0,))
        gate = RiskGate(RiskLimits(max_daily_loss=50.0))

        decision = gate.evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.DAILY_LOSS_LIMIT)

        below = RiskGate(RiskLimits(max_daily_loss=50.01)).evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)
        self.assertIs(below.decision, RiskDecisionType.ALLOW)

    def test_daily_loss_without_day_start_fails_closed(self) -> None:
        snapshot = _snapshot(position_qty=0.0, day_start_ts=None)
        gate = RiskGate(RiskLimits(max_daily_loss=10.0))

        decision = gate.evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.MISSING_DAILY_PNL)

    def test_drawdown_limit(self) -> None:
        # peak = 10000 + 200 = 10200；mark 回落到 100 → equity 10000 → drawdown ≈ 1.96%
        core = AccountingCore(initial_balance=10_000.0)
        core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
        core.update_mark_price(SYMBOL, 300.0, timestamp=BASE_TS + 1)
        core.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS + 2)
        snapshot = build_risk_snapshot(core, symbol=SYMBOL, now_ms=BASE_TS + 3)

        gate = RiskGate(RiskLimits(max_drawdown_pct=0.01))
        decision = gate.evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.DRAWDOWN_LIMIT)
        assert snapshot.drawdown_pct is not None
        self.assertAlmostEqual(snapshot.drawdown_pct, 200.0 / 10_200.0)

    def test_drawdown_within_limit_allows(self) -> None:
        core = AccountingCore(initial_balance=10_000.0)
        core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
        core.update_mark_price(SYMBOL, 110.0, timestamp=BASE_TS + 1)
        core.update_mark_price(SYMBOL, 105.0, timestamp=BASE_TS + 2)
        snapshot = build_risk_snapshot(core, symbol=SYMBOL, now_ms=BASE_TS + 3)

        gate = RiskGate(RiskLimits(max_drawdown_pct=0.5))
        decision = gate.evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)

        self.assertIs(decision.decision, RiskDecisionType.ALLOW)


class LiquidationGuardTest(unittest.TestCase):
    """SC-10：liquidation distance 过小时拒绝新增暴露；不自行计算强平价。"""

    def test_too_close_liquidation_is_rejected(self) -> None:
        snapshot = _snapshot(
            position_qty=1.0,
            mark=100.0,
            liquidation=LiquidationInfo.from_prices(symbol=SYMBOL, liquidation_price=99.5, mark_price=100.0),
        )
        gate = RiskGate(RiskLimits(min_liquidation_distance_bps=100.0))

        decision = gate.evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.LIQUIDATION_DISTANCE)
        self.assertIn("never computed here", decision.details)

    def test_safe_distance_allows(self) -> None:
        snapshot = _snapshot(
            position_qty=1.0,
            mark=100.0,
            liquidation=LiquidationInfo.from_prices(symbol=SYMBOL, liquidation_price=80.0, mark_price=100.0),
        )
        gate = RiskGate(RiskLimits(min_liquidation_distance_bps=100.0))

        decision = gate.evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)
        self.assertIs(decision.decision, RiskDecisionType.ALLOW)

    def test_reduce_only_ignores_liquidation_distance(self) -> None:
        snapshot = _snapshot(
            position_qty=1.0,
            mark=100.0,
            liquidation=LiquidationInfo.from_prices(symbol=SYMBOL, liquidation_price=99.9, mark_price=100.0),
        )
        gate = RiskGate(RiskLimits(min_liquidation_distance_bps=10_000.0))

        decision = gate.evaluate(_proposal(Side.SELL, 0.5, 100.0, reduce_only=True), snapshot)
        self.assertIs(decision.decision, RiskDecisionType.ALLOW)

    def test_configured_limit_without_liquidation_info_fails_closed(self) -> None:
        snapshot = _snapshot(position_qty=1.0, mark=100.0, liquidation=None)
        gate = RiskGate(RiskLimits(min_liquidation_distance_bps=100.0))

        decision = gate.evaluate(_proposal(Side.BUY, 0.1, 100.0), snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.MISSING_LIQUIDATION_INFO)

    def test_liquidation_info_validates_price_consistency(self) -> None:
        with self.assertRaises(InvalidLiquidationInfoError):
            LiquidationInfo(symbol=SYMBOL, liquidation_price=90.0, mark_price=100.0, distance_bps=123.0)
        with self.assertRaises(InvalidLiquidationInfoError):
            LiquidationInfo.from_prices(symbol=SYMBOL, liquidation_price=0.0, mark_price=100.0)

    def test_risk_never_computes_liquidation_price(self) -> None:
        import inspect

        import risk.gate as gate_module
        import risk.snapshot as snapshot_module

        for module in (gate_module, snapshot_module):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("maintenance_margin", source)
                self.assertNotIn("maintMargin", source)


if __name__ == "__main__":
    unittest.main()
