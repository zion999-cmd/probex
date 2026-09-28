"""Integration：Accounting → RiskSnapshot → RiskGate 的完整链路。

验证「Fill 入账 → 持仓/PnL/Equity 变化 → 风险快照 → 闸门判定」在真实组合下自洽，
以及 §11 的 reduce-only 与 increasing 的分流在真实数据上成立。
"""

from __future__ import annotations

import unittest

from portfolio.accounting import AccountingCore
from portfolio.types import LiquidationInfo, Side
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import OrderProposal, RiskDecisionType, RiskReasonCode
from tests.support import BASE_TS, SYMBOL, make_fill, make_funding


class AccountingRiskIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.core = AccountingCore(initial_balance=10_000.0)
        self.limits = RiskLimits(
            max_position_qty=0.5,
            max_position_notional=50_000.0,
            max_open_order_exposure=1_000.0,
            max_daily_loss=500.0,
            max_drawdown_pct=0.25,
            max_leverage=3.0,
            min_liquidation_distance_bps=200.0,
            max_mark_age_ms=5_000,
        )
        self.gate = RiskGate(self.limits)

    def _snapshot(self, *, now_ms: int = BASE_TS, liquidation: LiquidationInfo | None = None):
        return build_risk_snapshot(
            self.core,
            symbol=SYMBOL,
            now_ms=now_ms,
            open_order_exposure=100.0,
            liquidation=liquidation
            or LiquidationInfo.from_prices(symbol=SYMBOL, liquidation_price=80.0, mark_price=100.0),
            day_start_ts=utc_day_start_ms(now_ms),
        )

    def _proposal(self, side: Side, quantity: float, price: float, *, reduce_only: bool = False) -> OrderProposal:
        return OrderProposal(
            symbol=SYMBOL, side=side, quantity=quantity, price=price, reduce_only=reduce_only
        )

    def test_flat_account_allows_a_small_increasing_order(self) -> None:
        # 真实系统对每个 symbol 都有 mark；空仓也需要 mark 才能评估名义风险
        self.core.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS)
        snapshot = build_risk_snapshot(
            self.core,
            symbol=SYMBOL,
            now_ms=BASE_TS,
            open_order_exposure=0.0,
            liquidation=LiquidationInfo.from_prices(symbol=SYMBOL, liquidation_price=80.0, mark_price=100.0),
            day_start_ts=utc_day_start_ms(BASE_TS),
        )

        decision = self.gate.evaluate(self._proposal(Side.BUY, 0.2, 100.0), snapshot)

        self.assertIs(decision.decision, RiskDecisionType.ALLOW)
        self.assertIn("position_qty", decision.checks_applied)
        self.assertEqual(snapshot.equity, 10_000.0)  # 空仓 + mark → equity = balance
        self.assertEqual(snapshot.position_notional, 0.0)

    def test_full_flow_blocks_and_then_allows_a_reduction(self) -> None:
        # 建仓 + mark，使快照完整
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 0.5, fee=1.0))
        self.core.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS + 10)
        snapshot = self._snapshot(now_ms=BASE_TS + 20)

        # 满仓：继续加仓被 position limit 拒绝
        blocked = self.gate.evaluate(self._proposal(Side.BUY, 0.1, 100.0), snapshot)
        self.assertIs(blocked.reason_code, RiskReasonCode.POSITION_LIMIT)

        # 但 reduce-only 平掉一半必须被放行
        allowed = self.gate.evaluate(self._proposal(Side.SELL, 0.25, 100.0, reduce_only=True), snapshot)
        self.assertIs(allowed.decision, RiskDecisionType.ALLOW)

        # 平仓后快照与判定都自洽
        outcome = self.core.record_fill(make_fill("f2", Side.SELL, 110.0, 0.25, fee=0.5, exchange_ts=BASE_TS + 30))
        self.assertEqual(outcome.realized_delta, 2.5)
        after = self._snapshot(now_ms=BASE_TS + 40)
        self.assertEqual(after.position_qty, 0.25)
        # net = realized 2.5 − fees (1.0 + 0.5) = 1.0
        self.assertEqual(after.realized_pnl_today, 1.0)

    def test_daily_loss_from_funding_and_fees_blocks_new_exposure(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 2.0, fee=10.0))
        self.core.record_fill(make_fill("f2", Side.SELL, 95.0, 2.0, fee=10.0, exchange_ts=BASE_TS + 1))
        self.core.record_funding(make_funding(-500.0, timestamp=BASE_TS + 2))
        self.core.update_mark_price(SYMBOL, 95.0, timestamp=BASE_TS + 3)
        snapshot = self._snapshot(now_ms=BASE_TS + 4)

        self.assertEqual(snapshot.position_qty, 0.0)
        self.assertEqual(snapshot.realized_pnl_today, -530.0)  # -10 trade -20 fees -500 funding

        decision = self.gate.evaluate(self._proposal(Side.BUY, 0.1, 100.0), snapshot)
        self.assertIs(decision.reason_code, RiskReasonCode.DAILY_LOSS_LIMIT)

    def test_mark_move_does_not_change_realized_but_moves_drawdown(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 0.3))
        self.core.update_mark_price(SYMBOL, 200.0, timestamp=BASE_TS + 1)
        peak_snapshot = self._snapshot(now_ms=BASE_TS + 2)
        realized_at_peak = self.core.realized_trade_pnl

        self.core.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS + 3)
        dropped = self._snapshot(now_ms=BASE_TS + 4)

        self.assertEqual(self.core.realized_trade_pnl, realized_at_peak)
        self.assertEqual(peak_snapshot.drawdown, 0.0)
        assert dropped.drawdown is not None and peak_snapshot.equity is not None
        self.assertAlmostEqual(dropped.drawdown, 30.0)  # 0.3 * (200-100)

        # 高 drawdown 下仍然允许 reduce-only
        decision = self.gate.evaluate(self._proposal(Side.SELL, 0.1, 100.0, reduce_only=True), dropped)
        self.assertIs(decision.decision, RiskDecisionType.ALLOW)

    def test_liquidation_guard_uses_adapter_supplied_prices(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 0.4))
        self.core.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS + 1)

        safe = self._snapshot(now_ms=BASE_TS + 2, liquidation=LiquidationInfo.from_prices(
            symbol=SYMBOL, liquidation_price=80.0, mark_price=100.0
        ))
        safe_decision = self.gate.evaluate(self._proposal(Side.BUY, 0.01, 100.0), safe)
        self.assertIs(safe_decision.decision, RiskDecisionType.ALLOW)

        risky = self._snapshot(now_ms=BASE_TS + 2, liquidation=LiquidationInfo.from_prices(
            symbol=SYMBOL, liquidation_price=99.0, mark_price=100.0
        ))
        risky_decision = self.gate.evaluate(self._proposal(Side.BUY, 0.01, 100.0), risky)
        self.assertIs(risky_decision.reason_code, RiskReasonCode.LIQUIDATION_DISTANCE)

    def test_stale_guard_uses_mark_timestamps(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 0.1))
        self.core.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS)

        fresh = self._snapshot(now_ms=BASE_TS + 1_000)
        self.assertIs(self.gate.evaluate(self._proposal(Side.BUY, 0.01, 100.0), fresh).decision, RiskDecisionType.ALLOW)

        stale = self._snapshot(now_ms=BASE_TS + 10_000)
        self.assertIs(self.gate.evaluate(self._proposal(Side.BUY, 0.01, 100.0), stale).reason_code, RiskReasonCode.STALE_MARK)


if __name__ == "__main__":
    unittest.main()
