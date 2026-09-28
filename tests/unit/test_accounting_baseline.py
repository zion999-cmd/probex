"""P0001.9.3 单元测试：AccountingCore 一次性 startup baseline（SC-6 / SC-7）。"""

from __future__ import annotations

import unittest

from portfolio.accounting import AccountingCore, BaselineAlreadyAppliedError, BaselineApplication
from portfolio.types import ExternalAccountBaseline, InvalidBaselineError, Side
from tests.support import BASE_TS, make_fill


def _baseline(**overrides: object) -> ExternalAccountBaseline:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "wallet_balance": 5_000.0,
        "available_balance": 4_900.0,
        "position_qty": 0.5,
        "entry_price": 60_000.0,
        "mark_price": 61_000.0,
        "liquidation_price": 50_000.0,
        "captured_at": BASE_TS,
    }
    values.update(overrides)
    return ExternalAccountBaseline(**values)  # type: ignore[arg-type]


class BaselineApplicationTest(unittest.TestCase):
    def test_sc6_bootstrap_restores_state_without_synthetic_fill(self) -> None:
        accounting = AccountingCore(initial_balance=0.0)

        applied = accounting.bootstrap_from_baseline(_baseline())

        self.assertIsInstance(applied, BaselineApplication)
        self.assertEqual(len(accounting.fills.fills), 0)  # **没有** synthetic Fill
        self.assertAlmostEqual(accounting.balance, 5_000.0)
        self.assertAlmostEqual(accounting.equity() or 0.0, 5_500.0)  # 5000 + 0.5*(61000-60000)
        position = accounting.position("BTCUSDT")
        self.assertAlmostEqual(position.qty, 0.5)
        self.assertAlmostEqual(position.avg_entry_price, 60_000.0)
        self.assertAlmostEqual(position.mark_price or 0.0, 61_000.0)
        self.assertEqual(accounting.mark_timestamp("BTCUSDT"), BASE_TS)
        self.assertTrue(accounting.baseline_applied)

    def test_sc7_second_bootstrap_is_refused(self) -> None:
        accounting = AccountingCore(initial_balance=0.0)
        accounting.bootstrap_from_baseline(_baseline())

        with self.assertRaises(BaselineAlreadyAppliedError):
            accounting.bootstrap_from_baseline(_baseline(wallet_balance=9_999.0))

        self.assertAlmostEqual(accounting.balance, 5_000.0)  # 未被第二次覆盖

    def test_bootstrap_after_session_fills_is_refused(self) -> None:
        accounting = AccountingCore(initial_balance=1_000.0)
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))

        with self.assertRaises(BaselineAlreadyAppliedError):
            accounting.bootstrap_from_baseline(_baseline())

    def test_flat_baseline_requires_zero_entry_price(self) -> None:
        accounting = AccountingCore(initial_balance=0.0)

        applied = accounting.bootstrap_from_baseline(
            _baseline(position_qty=0.0, entry_price=0.0, mark_price=0.0, liquidation_price=0.0)
        )

        self.assertTrue(applied.position.is_flat)
        self.assertAlmostEqual(accounting.balance, 5_000.0)

    def test_invalid_baseline_rejected(self) -> None:
        with self.assertRaises(InvalidBaselineError):
            _baseline(mark_price=0.0)  # 有仓位却没有 mark ⇒ fail closed
        with self.assertRaises(InvalidBaselineError):
            _baseline(position_qty=0.5, entry_price=0.0)
        # 空仓带真实行情价（测试网实测）合法；空仓带非 0 entryPrice 非法
        _baseline(position_qty=0.0, entry_price=0.0, mark_price=61_000.0, liquidation_price=0.0)
        with self.assertRaises(InvalidBaselineError):
            _baseline(position_qty=0.0, entry_price=60_000.0, mark_price=61_000.0, liquidation_price=0.0)
        with self.assertRaises(InvalidBaselineError):
            _baseline(wallet_balance=float("nan"))
        with self.assertRaises(InvalidBaselineError):
            _baseline(symbol="")

    def test_balance_only_moves_with_real_flows_after_bootstrap(self) -> None:
        accounting = AccountingCore(initial_balance=0.0)
        accounting.bootstrap_from_baseline(_baseline())

        accounting.record_fill(make_fill("f1", Side.BUY, 61_000.0, 0.1, fee=0.61))

        self.assertAlmostEqual(accounting.balance, 5_000.0 - 0.61)
        self.assertAlmostEqual(accounting.position("BTCUSDT").qty, 0.6)


class UnknownHistoricalPnlTest(unittest.TestCase):
    def test_sc6_historical_pnl_is_unknown_after_baseline(self) -> None:
        accounting = AccountingCore(initial_balance=0.0)
        accounting.bootstrap_from_baseline(_baseline())

        self.assertFalse(accounting.historical_pnl_known)
        # 窗口起点早于 baseline ⇒ 未知（不能假装是 0）
        self.assertIsNone(accounting.net_realized_since(BASE_TS - 1))
        # 窗口起点在 baseline 之后 ⇒ 由账本给出（本 session 已知）
        self.assertEqual(accounting.net_realized_since(BASE_TS + 1), 0.0)

    def test_historical_peak_is_unknown_after_baseline(self) -> None:
        accounting = AccountingCore(initial_balance=0.0)
        accounting.bootstrap_from_baseline(_baseline())

        self.assertIsNone(accounting.peak_equity)
        self.assertIsNone(accounting.drawdown())

        accounting.update_mark_price("BTCUSDT", 60_000.0, timestamp=BASE_TS + 10)

        self.assertIsNone(accounting.peak_equity)  # 仍未知：不允许把 session 起点冒充历史峰值
        self.assertIsNone(accounting.drawdown())

    def test_without_baseline_semantics_are_unchanged(self) -> None:
        accounting = AccountingCore(initial_balance=1_000.0)
        accounting.update_mark_price("BTCUSDT", 100.0, timestamp=BASE_TS)
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=1.0))

        self.assertTrue(accounting.historical_pnl_known)
        self.assertEqual(accounting.net_realized_since(0), -1.0)
        self.assertIsNotNone(accounting.peak_equity)  # 无 baseline ⇒ 峰值照常记录


if __name__ == "__main__":
    unittest.main()
