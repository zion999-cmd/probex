"""Funding：独立入账、影响 balance/equity/net_realized，但不是 Fill / Fee / realized。"""

from __future__ import annotations

import unittest

from portfolio.funding import FundingLedger
from portfolio.types import FundingPayment, InvalidFundingError, UnsupportedAssetError
from tests.support import BASE_TS, make_fill, make_funding, make_funding as funding
from portfolio.accounting import AccountingCore
from portfolio.types import Side


class FundingPaymentTest(unittest.TestCase):
    def test_valid_payment(self) -> None:
        payment = make_funding(-1.25, rate=0.0001)
        self.assertEqual(payment.amount, -1.25)
        self.assertEqual(payment.asset, "USDT")
        self.assertEqual(payment.rate, 0.0001)

    def test_invalid_payments_rejected(self) -> None:
        with self.assertRaises(InvalidFundingError):
            FundingPayment(symbol="", amount=1.0, asset="USDT", timestamp=1)
        with self.assertRaises(InvalidFundingError):
            FundingPayment(symbol="BTCUSDT", amount=1.0, asset="", timestamp=1)
        with self.assertRaises(InvalidFundingError):
            FundingPayment(symbol="BTCUSDT", amount=float("inf"), asset="USDT", timestamp=1)
        with self.assertRaises(InvalidFundingError):
            FundingPayment(symbol="BTCUSDT", amount=1.0, asset="USDT", timestamp=-1)

    def test_payment_is_immutable(self) -> None:
        with self.assertRaises(Exception):
            make_funding(1.0).amount = 2.0  # type: ignore[misc]


class FundingLedgerTest(unittest.TestCase):
    def test_totals_and_windows(self) -> None:
        ledger = FundingLedger()
        ledger.record(make_funding(-1.0, timestamp=BASE_TS))
        ledger.record(make_funding(0.25, timestamp=BASE_TS + 1_000))
        ledger.record(make_funding(-0.5, timestamp=BASE_TS + 2_000))

        self.assertEqual(len(ledger), 3)
        self.assertEqual(ledger.total, -1.25)
        self.assertEqual(ledger.total_since(BASE_TS + 1_000), -0.25)
        self.assertEqual(ledger.for_symbol("BTCUSDT")[0].amount, -1.0)
        self.assertEqual(ledger.for_symbol("ETHUSDT"), ())

    def test_non_settlement_asset_rejected(self) -> None:
        ledger = FundingLedger(settlement_asset="USDT")
        with self.assertRaises(UnsupportedAssetError):
            ledger.record(funding(1.0, asset="USDC"))
        self.assertEqual(len(ledger), 0)


class FundingAccountingTest(unittest.TestCase):
    def test_funding_moves_balance_and_equity_but_not_realized(self) -> None:
        core = AccountingCore(initial_balance=100.0)
        core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
        core.update_mark_price("BTCUSDT", 100.0, timestamp=BASE_TS + 1)

        realized_before = core.realized_trade_pnl
        equity_before = core.equity()

        core.record_funding(make_funding(-5.0, timestamp=BASE_TS + 2))

        self.assertEqual(core.realized_trade_pnl, realized_before)
        self.assertEqual(core.trading_fees, 0.0)
        self.assertEqual(core.funding_total, -5.0)
        self.assertEqual(core.balance, 100.0 - 5.0)
        self.assertEqual(core.equity(), (equity_before or 0.0) - 5.0)
        self.assertEqual(core.net_realized, -5.0)

    def test_positive_funding_increases_balance(self) -> None:
        core = AccountingCore(initial_balance=100.0)
        core.record_funding(make_funding(3.0))

        self.assertEqual(core.balance, 103.0)
        self.assertEqual(core.net_realized, 3.0)
        self.assertEqual(core.equity(), 103.0)


if __name__ == "__main__":
    unittest.main()
