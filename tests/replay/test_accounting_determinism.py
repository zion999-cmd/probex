"""Replay：同一组 Fill / Funding / Mark 重放两次必须完全一致（SC-1 / SC-12）。

Replay 不依赖 wall-clock：所有时间戳都由事件或调用方注入（`now_ms` / `day_start_ts`），
因此「同一序列 + 同一注入时间」必然产生逐字段一致的状态与快照。
"""

from __future__ import annotations

import unittest

from portfolio.accounting import AccountingCore
from portfolio.types import LiquidationInfo, Side
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import OrderProposal, RiskSnapshot
from tests.support import BASE_TS, SYMBOL, make_fill, make_funding

#: 一段固定的账户事件脚本（含加仓、部分平仓、反手、手续费、资金费、mark 变化）。
FILL_SCRIPT = (
    ("f1", Side.BUY, 100.0, 2.0, 0.20, "t1", BASE_TS),
    ("f2", Side.BUY, 102.0, 1.0, 0.10, "t2", BASE_TS + 1_000),
    ("f3", Side.SELL, 110.0, 2.0, 0.20, "t3", BASE_TS + 2_000),
    ("f4", Side.SELL, 120.0, 2.0, 0.20, "t4", BASE_TS + 3_000),
    ("f5", Side.BUY, 115.0, 0.5, 0.10, "t5", BASE_TS + 4_000),  # 部分回补：最终 short 0.5 @120
)

MARK_SCRIPT = (
    (101.0, BASE_TS + 500),
    (105.0, BASE_TS + 1_500),
    (95.0, BASE_TS + 2_500),
    (118.0, BASE_TS + 3_500),
    (112.0, BASE_TS + 4_500),
)

FUNDING_SCRIPT = (
    (-1.5, BASE_TS + 1_200),
    (0.75, BASE_TS + 3_200),
)


def _replay(*, duplicate_delivery: bool = False, now_ms: int = BASE_TS + 5_000) -> tuple:
    """跑一遍脚本，返回用于比较的 canonical 状态。"""
    core = AccountingCore(initial_balance=10_000.0)
    for index, (fill_id, side, price, qty, fee, trade_id, ts) in enumerate(FILL_SCRIPT):
        fill = make_fill(fill_id, side, price, qty, fee=fee, trade_id=trade_id, exchange_ts=ts)
        core.record_fill(fill)
        if duplicate_delivery:
            core.record_fill(fill)
    for amount, ts in FUNDING_SCRIPT:
        core.record_funding(make_funding(amount, timestamp=ts))
    for price, ts in MARK_SCRIPT:
        core.update_mark_price(SYMBOL, price, timestamp=ts)

    snapshot = build_risk_snapshot(
        core,
        symbol=SYMBOL,
        now_ms=now_ms,
        open_order_exposure=250.0,
        liquidation=LiquidationInfo.from_prices(symbol=SYMBOL, liquidation_price=90.0, mark_price=112.0),
        day_start_ts=utc_day_start_ms(now_ms),
    )
    gate_decision = RiskGate(
        RiskLimits(max_position_qty=10.0, max_position_notional=10_000.0, max_leverage=5.0, max_daily_loss=1_000.0)
    ).evaluate(OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=1.0, price=112.0), snapshot)

    position = core.position(SYMBOL)
    return (
        position.qty,
        position.avg_entry_price,
        position.realized_pnl,
        core.realized_trade_pnl,
        core.trading_fees,
        core.funding_total,
        core.net_realized,
        core.balance,
        core.unrealized_pnl(),
        core.equity(),
        core.peak_equity,
        core.drawdown(),
        core.gross_exposure,
        core.net_exposure,
        core.fills.count,
        core.fills.duplicate_count,
        snapshot,
        gate_decision,
    )


class AccountingDeterminismTest(unittest.TestCase):
    def test_sc1_two_replays_are_field_identical(self) -> None:
        first = _replay()
        second = _replay()

        self.assertEqual(first, second)

        # 脚本语义（用于防止「测试空跑」）：long 3 → 部分平 2 → 反手 short 1 @120 → 回补 0.5
        snapshot: RiskSnapshot = first[16]
        self.assertAlmostEqual(first[0], -0.5)  # position qty
        self.assertAlmostEqual(first[1], 120.0)  # avg entry（反手后以成交价为准）
        self.assertAlmostEqual(first[2], 40.5)  # realized trade pnl
        self.assertAlmostEqual(first[4], 0.80, places=8)  # fees
        self.assertAlmostEqual(first[5], -0.75, places=8)  # funding
        self.assertAlmostEqual(first[8] or 0.0, 4.0, places=8)  # unrealized = (112-120) * -0.5
        self.assertAlmostEqual(first[7], 10_038.95, places=6)  # balance
        self.assertAlmostEqual(snapshot.position_notional or 0.0, 56.0, places=6)
        self.assertAlmostEqual(snapshot.gross_exposure or 0.0, 56.0, places=6)

    def test_replay_is_stable_across_instances(self) -> None:
        states = [_replay() for _ in range(3)]
        self.assertEqual(states[0], states[1])
        self.assertEqual(states[1], states[2])

    def test_sc2_duplicate_delivery_yields_the_same_state(self) -> None:
        # 每个 Fill 交付两次（重放/补推）→ 最终状态与单次交付完全一致
        single = _replay()
        duplicated = _replay(duplicate_delivery=True)

        self.assertEqual(duplicated[:15], single[:15])  # 账户事实
        self.assertEqual(duplicated[15], len(FILL_SCRIPT))  # duplicate_count
        self.assertEqual(duplicated[16], single[16])  # 快照
        self.assertEqual(duplicated[17], single[17])  # 闸门判定

    def test_accounting_state_is_independent_of_snapshot_time(self) -> None:
        early = _replay(now_ms=BASE_TS + 5_000)
        late = _replay(now_ms=BASE_TS + 500_000)

        self.assertEqual(early[:15], late[:15])  # 账户事实不受 now_ms 影响
        early_snapshot: RiskSnapshot = early[16]
        late_snapshot: RiskSnapshot = late[16]
        self.assertNotEqual(early_snapshot.mark_age_ms, late_snapshot.mark_age_ms)
        self.assertEqual(early_snapshot.balance, late_snapshot.balance)
        self.assertEqual(early_snapshot.equity, late_snapshot.equity)
        self.assertEqual(late_snapshot.mark_age_ms, 500_000 - 4_500)

    def test_unknown_mark_stays_unknown_across_replays(self) -> None:
        def replay_without_mark() -> tuple:
            core = AccountingCore(initial_balance=100.0)
            core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
            snapshot = build_risk_snapshot(core, symbol=SYMBOL, now_ms=BASE_TS)
            return (core.unrealized_pnl(), core.equity(), snapshot.mark_price, snapshot.equity)

        self.assertEqual(replay_without_mark(), replay_without_mark())
        self.assertEqual(replay_without_mark(), (None, None, None, None))

    def test_replay_has_no_wall_clock_dependency(self) -> None:
        # 结构性保证：portfolio/** 与 risk/** 不 import time / datetime（见 test_accounting_risk_isolation）
        import portfolio.accounting as accounting_module
        import risk.snapshot as snapshot_module

        for module in (accounting_module, snapshot_module):
            with self.subTest(module=module.__name__):
                source = __import__("inspect").getsource(module)
                self.assertNotIn("time.time", source)
                self.assertNotIn("datetime", source)


if __name__ == "__main__":
    unittest.main()
