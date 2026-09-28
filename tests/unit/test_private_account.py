"""P0001.9.2 单元测试：账户快照归一化（SC-4 / SC-5）。"""

from __future__ import annotations

import unittest

from connectors.binance.private.account import parse_account_snapshot
from connectors.binance.private.errors import PrivateFormatError, UnsupportedAccountModeError
from tests.private_support import SYMBOL, account_payload


def _parse(**overrides: object):
    payload = account_payload(**overrides)  # type: ignore[arg-type]
    return parse_account_snapshot(payload, symbol=SYMBOL, receive_ts=100, process_ts=200)


class AccountSnapshotTest(unittest.TestCase):
    def test_sc4_balances_and_totals_are_normalized(self) -> None:
        snapshot = _parse(wallet_balance="1234.56", available_balance="1000.00")

        self.assertEqual(snapshot.symbol, SYMBOL)
        self.assertTrue(snapshot.can_trade)
        self.assertAlmostEqual(snapshot.total_wallet_balance, 1234.56)
        self.assertAlmostEqual(snapshot.available_balance, 1000.00)
        self.assertAlmostEqual(snapshot.total_margin_balance, 1001.50)
        self.assertAlmostEqual(snapshot.total_unrealized_profit, 1.00)
        self.assertAlmostEqual(snapshot.max_withdraw_amount or 0.0, 800.00)
        self.assertEqual(snapshot.update_time_ms, 1_700_000_000_000)
        self.assertEqual((snapshot.receive_ts, snapshot.process_ts), (100, 200))

    def test_settlement_balance_lookup(self) -> None:
        snapshot = _parse()

        settlement = snapshot.settlement_balance
        self.assertIsNotNone(settlement)
        self.assertTrue(settlement.is_settlement_asset)
        self.assertAlmostEqual(settlement.balance, 1000.50)
        self.assertAlmostEqual(settlement.available_balance, 900.25)
        self.assertAlmostEqual(settlement.wallet_balance, 1000.50)
        self.assertAlmostEqual(settlement.margin_balance, 1001.50)
        self.assertAlmostEqual(settlement.unrealized_profit, 1.00)
        self.assertIsNone(snapshot.balance("DOGE"))

    def test_sc5_hedge_mode_position_side_fails_closed(self) -> None:
        with self.assertRaises(UnsupportedAccountModeError):
            _parse(position_side="LONG")

    def test_sc5_dual_side_position_flag_fails_closed(self) -> None:
        with self.assertRaises(UnsupportedAccountModeError):
            _parse(dual_side_position=True)

    def test_dual_side_position_absent_is_recorded_as_none(self) -> None:
        snapshot = _parse()

        self.assertIsNone(snapshot.dual_side_position)
        self.assertEqual(snapshot.position_sides, ((SYMBOL, "BOTH"),))

    def test_missing_required_fields_fail_closed(self) -> None:
        for field in ("canTrade", "totalWalletBalance", "totalMarginBalance", "totalUnrealizedProfit", "availableBalance", "assets", "positions"):
            payload = account_payload()
            del payload[field]
            with self.subTest(field=field):
                with self.assertRaises(PrivateFormatError):
                    parse_account_snapshot(payload, symbol=SYMBOL, receive_ts=1, process_ts=1)

    def test_invalid_numbers_fail_closed(self) -> None:
        for value in ("abc", None, True, ""):
            payload = account_payload()
            payload["totalWalletBalance"] = value
            with self.subTest(value=value):
                with self.assertRaises(PrivateFormatError):
                    parse_account_snapshot(payload, symbol=SYMBOL, receive_ts=1, process_ts=1)

    def test_empty_assets_fail_closed(self) -> None:
        payload = account_payload()
        payload["assets"] = []

        with self.assertRaises(PrivateFormatError):
            parse_account_snapshot(payload, symbol=SYMBOL, receive_ts=1, process_ts=1)

    def test_balance_entry_requires_all_balance_fields(self) -> None:
        payload = account_payload()
        del payload["assets"][0]["marginBalance"]  # type: ignore[index]

        with self.assertRaises(PrivateFormatError):
            parse_account_snapshot(payload, symbol=SYMBOL, receive_ts=1, process_ts=1)

    def test_non_trading_account_is_still_readable(self) -> None:
        snapshot = _parse(can_trade=False)

        self.assertFalse(snapshot.can_trade)


if __name__ == "__main__":
    unittest.main()
