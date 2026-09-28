"""Accounting Core：Balance ≠ Equity、Fee / Funding / Realized 分离、mark 只影响 unrealized。

覆盖 SC-4、SC-5、SC-6。
"""

from __future__ import annotations

import unittest

from portfolio.accounting import AccountingCore
from portfolio.types import Side, UnsupportedAssetError
from tests.support import BASE_TS, make_fill, make_funding


class AccountingBalanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.core = AccountingCore(initial_balance=10_000.0)

    def test_initial_state(self) -> None:
        self.assertEqual(self.core.balance, 10_000.0)
        self.assertEqual(self.core.realized_trade_pnl, 0.0)
        self.assertEqual(self.core.trading_fees, 0.0)
        self.assertEqual(self.core.funding_total, 0.0)
        self.assertEqual(self.core.net_realized, 0.0)
        self.assertEqual(self.core.unrealized_pnl(), 0.0)
        self.assertEqual(self.core.equity(), 10_000.0)
        self.assertTrue(self.core.position("BTCUSDT").is_flat)

    def test_sc4_fee_funding_and_realized_trade_pnl_are_separate(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=0.5))
        self.core.record_fill(make_fill("f2", Side.SELL, 110.0, 1.0, fee=0.7, exchange_ts=BASE_TS + 10))
        self.core.record_funding(make_funding(-1.5))
        self.core.update_mark_price("BTCUSDT", 110.0, timestamp=BASE_TS + 20)

        self.assertEqual(self.core.realized_trade_pnl, 10.0)  # 只含交易盈亏
        self.assertEqual(self.core.trading_fees, 1.2)
        self.assertEqual(self.core.funding_total, -1.5)
        self.assertEqual(self.core.net_realized, 10.0 - 1.2 - 1.5)
        self.assertEqual(self.core.balance, 10_000.0 + 10.0 - 1.2 - 1.5)

    def test_balance_and_equity_differ_with_open_position(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 2.0))
        self.core.update_mark_price("BTCUSDT", 105.0, timestamp=BASE_TS + 1)

        self.assertEqual(self.core.balance, 10_000.0)
        self.assertEqual(self.core.unrealized_pnl(), 10.0)
        self.assertEqual(self.core.equity(), 10_010.0)

    def test_sc5_mark_changes_only_affect_unrealized_and_equity(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=1.0))
        self.core.update_mark_price("BTCUSDT", 120.0, timestamp=BASE_TS + 1)

        realized_before = self.core.realized_trade_pnl
        balance_before = self.core.balance
        equity_before = self.core.equity()

        self.core.update_mark_price("BTCUSDT", 80.0, timestamp=BASE_TS + 2)

        self.assertEqual(self.core.realized_trade_pnl, realized_before)
        self.assertEqual(self.core.balance, balance_before)
        self.assertNotEqual(self.core.equity(), equity_before)
        self.assertEqual(self.core.equity(), balance_before + (-20.0))

    def test_sc6_flat_position_has_zero_unrealized(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
        self.core.update_mark_price("BTCUSDT", 150.0, timestamp=BASE_TS + 1)
        self.core.record_fill(make_fill("f2", Side.SELL, 150.0, 1.0, exchange_ts=BASE_TS + 2))

        position = self.core.position("BTCUSDT")
        self.assertTrue(position.is_flat)
        self.assertEqual(position.unrealized_pnl, 0.0)
        self.assertEqual(self.core.unrealized_pnl(), 0.0)
        self.assertEqual(self.core.equity(), self.core.balance)

    def test_unknown_mark_makes_unrealized_and_equity_unknown(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))

        self.assertIsNone(self.core.unrealized_pnl())
        self.assertIsNone(self.core.equity())
        self.assertEqual(self.core.balance, 10_000.0)  # balance 始终已知

    def test_non_settlement_asset_fee_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedAssetError):
            self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=0.1, fee_asset="BNB"))
        self.assertEqual(self.core.fills.count, 0)

    def test_settlement_asset_is_configurable(self) -> None:
        core = AccountingCore(settlement_asset="USDC", initial_balance=100.0)
        core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=0.1, fee_asset="USDC"))
        self.assertEqual(core.trading_fees, 0.1)

        with self.assertRaises(UnsupportedAssetError):
            core.record_fill(make_fill("f2", Side.BUY, 100.0, 1.0, fee=0.1, fee_asset="USDT"))


class AccountingPnlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.core = AccountingCore(initial_balance=1_000.0)

    def test_realized_trade_pnl_tracks_position_sums(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
        self.core.record_fill(make_fill("f2", Side.SELL, 110.0, 1.0, exchange_ts=BASE_TS + 1))
        self.core.record_fill(make_fill("f3", Side.SELL, 120.0, 1.0, exchange_ts=BASE_TS + 2))
        self.core.record_fill(make_fill("f4", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS + 3))

        # short 1 @120 → 平仓 @100 → +20
        self.assertEqual(self.core.realized_trade_pnl, 30.0)

    def test_daily_windows_use_injected_timestamps(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=1.0, exchange_ts=BASE_TS))
        self.core.record_fill(make_fill("f2", Side.SELL, 110.0, 1.0, fee=1.0, exchange_ts=BASE_TS + 5_000))
        self.core.record_funding(make_funding(-2.0, timestamp=BASE_TS + 6_000))

        self.assertEqual(self.core.realized_trade_pnl_since(BASE_TS), 10.0)
        self.assertEqual(self.core.trading_fees_since(BASE_TS), 2.0)
        self.assertEqual(self.core.realized_trade_pnl_since(BASE_TS + 1_000), 10.0)
        self.assertEqual(self.core.trading_fees_since(BASE_TS + 1_000), 1.0)
        self.assertEqual(self.core.funding_since(BASE_TS + 1_000), -2.0)
        self.assertEqual(self.core.net_realized_since(BASE_TS + 1_000), 10.0 - 1.0 - 2.0)
        self.assertEqual(self.core.net_realized_since(BASE_TS + 10_000), 0.0)

    def test_peak_equity_and_drawdown(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
        self.core.update_mark_price("BTCUSDT", 200.0, timestamp=BASE_TS + 1)
        self.assertEqual(self.core.peak_equity, 1_100.0)
        self.assertEqual(self.core.drawdown(), 0.0)

        self.core.update_mark_price("BTCUSDT", 150.0, timestamp=BASE_TS + 2)
        self.assertEqual(self.core.peak_equity, 1_100.0)
        self.assertEqual(self.core.drawdown(), 50.0)

    def test_drawdown_is_unknown_before_any_equity_observation(self) -> None:
        self.assertIsNone(self.core.peak_equity)
        self.assertIsNone(self.core.drawdown())

    def test_exposures_follow_mark(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 3.0))
        self.assertIsNone(self.core.gross_exposure)

        self.core.update_mark_price("BTCUSDT", 50.0, timestamp=BASE_TS + 1)
        self.assertEqual(self.core.gross_exposure, 150.0)
        self.assertEqual(self.core.net_exposure, 150.0)

        self.core.record_fill(make_fill("f2", Side.SELL, 50.0, 3.0, exchange_ts=BASE_TS + 2))
        self.assertEqual(self.core.gross_exposure, 0.0)
        self.assertEqual(self.core.net_exposure, 0.0)


class AccountingValidationTest(unittest.TestCase):
    def test_invalid_configuration(self) -> None:
        with self.assertRaises(ValueError):
            AccountingCore(settlement_asset="")
        with self.assertRaises(ValueError):
            AccountingCore(initial_balance=float("nan"))  # type: ignore[arg-type]

    def test_invalid_mark_updates(self) -> None:
        core = AccountingCore()
        for price in (0.0, -1.0, float("nan")):
            with self.subTest(price=price):
                with self.assertRaises(ValueError):
                    core.update_mark_price("BTCUSDT", price, timestamp=1)
        for timestamp in (-1, 1.5):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    core.update_mark_price("BTCUSDT", 100.0, timestamp=timestamp)  # type: ignore[arg-type]

    def test_mark_timestamp_is_recorded(self) -> None:
        core = AccountingCore()
        core.update_mark_price("BTCUSDT", 100.0, timestamp=1_234)

        self.assertEqual(core.mark_price("BTCUSDT"), 100.0)
        self.assertEqual(core.mark_timestamp("BTCUSDT"), 1_234)
        self.assertIsNone(core.mark_price("ETHUSDT"))


if __name__ == "__main__":
    unittest.main()
