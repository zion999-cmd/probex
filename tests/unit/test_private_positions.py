"""P0001.9.2 单元测试：持仓事实与 fail-closed 模式校验（SC-4 / SC-5）。"""

from __future__ import annotations

import unittest

from connectors.binance.private.errors import PrivateFormatError, UnsupportedAccountModeError
from connectors.binance.private.positions import AccountMode, parse_position_entry, parse_position_risk
from tests.private_support import SYMBOL, position_risk_payload, testnet_position_risk_payload


class PositionRiskTest(unittest.TestCase):
    def test_sc4_full_position_facts(self) -> None:
        position = parse_position_risk(position_risk_payload(), symbol=SYMBOL, receive_ts=10, process_ts=20)

        self.assertEqual(position.symbol, SYMBOL)
        self.assertAlmostEqual(position.position_amt, 0.5)
        self.assertAlmostEqual(position.entry_price, 60000.10)
        self.assertAlmostEqual(position.mark_price, 60100.00)
        self.assertAlmostEqual(position.unrealized_profit, 49.95)
        self.assertAlmostEqual(position.liquidation_price, 55000.00)
        self.assertEqual(position.leverage, 10)
        self.assertEqual(position.margin_type, "cross")
        self.assertEqual(position.margin_asset, "USDT")
        self.assertEqual(position.update_time_ms, 1_700_000_000_000)
        self.assertEqual((position.receive_ts, position.process_ts), (10, 20))
        self.assertIs(position.account_mode, AccountMode.ONE_WAY)

    def test_direction_helpers_and_notional(self) -> None:
        long_position = parse_position_risk(position_risk_payload(), symbol=SYMBOL, receive_ts=1, process_ts=1)
        short_position = parse_position_risk(
            position_risk_payload(position_amt="-0.25"), symbol=SYMBOL, receive_ts=1, process_ts=1
        )
        flat_position = parse_position_risk(
            position_risk_payload(position_amt="0", entry_price="0", liquidation_price="0"),
            symbol=SYMBOL,
            receive_ts=1,
            process_ts=1,
        )

        self.assertTrue(long_position.is_long)
        self.assertTrue(short_position.is_short)
        self.assertTrue(flat_position.is_flat)
        self.assertAlmostEqual(short_position.notional, 0.25 * 60100.00)

    def test_sc5_hedge_mode_fails_closed(self) -> None:
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                with self.assertRaises(UnsupportedAccountModeError):
                    parse_position_risk(
                        position_risk_payload(position_side=side), symbol=SYMBOL, receive_ts=1, process_ts=1
                    )

    def test_non_usdt_margin_fails_closed(self) -> None:
        with self.assertRaises(UnsupportedAccountModeError):
            parse_position_risk(
                position_risk_payload(margin_asset="BUSD"), symbol=SYMBOL, receive_ts=1, process_ts=1
            )

    def test_missing_margin_asset_is_allowed_with_none(self) -> None:
        """真实测试网 payload 没有 marginAsset/asset ⇒ 允许缺失，记录为 None（USDT-M 由端点+账户资产确认）。"""
        entry = position_risk_payload()[0]
        del entry["marginAsset"]

        position = parse_position_entry(entry, receive_ts=1, process_ts=1)

        self.assertIsNone(position.margin_asset)
        self.assertIs(position.account_mode, AccountMode.ONE_WAY)

    def test_real_testnet_shape_parses(self) -> None:
        """测试网形态：字符串 leverage + 无 marginAsset + 空仓 markPrice="0"。"""
        position = parse_position_risk(
            testnet_position_risk_payload(), symbol=SYMBOL, receive_ts=1, process_ts=1
        )

        self.assertEqual(position.leverage, 20)
        self.assertIsNone(position.margin_asset)
        self.assertEqual(position.mark_price, 0.0)
        self.assertTrue(position.is_flat)
        self.assertEqual(position.notional, 0.0)

    def test_string_leverage_is_accepted(self) -> None:
        position = parse_position_risk(
            position_risk_payload(leverage="5"), symbol=SYMBOL, receive_ts=1, process_ts=1
        )

        self.assertEqual(position.leverage, 5)

    def test_zero_mark_price_on_open_position_fails_closed(self) -> None:
        with self.assertRaises(PrivateFormatError):
            parse_position_risk(
                position_risk_payload(position_amt="0.5", mark_price="0"), symbol=SYMBOL, receive_ts=1, process_ts=1
            )

    def test_invalid_string_leverage_fails_closed(self) -> None:
        for value in ("abc", "2.5", "", True, "1e2"):
            with self.subTest(value=value):
                with self.assertRaises(PrivateFormatError):
                    parse_position_risk(
                        position_risk_payload(leverage=value), symbol=SYMBOL, receive_ts=1, process_ts=1  # type: ignore[arg-type]
                    )

    def test_symbol_must_be_present_in_response(self) -> None:
        with self.assertRaises(PrivateFormatError):
            parse_position_risk(position_risk_payload(symbol="ETHUSDT"), symbol=SYMBOL, receive_ts=1, process_ts=1)

    def test_response_must_be_an_array(self) -> None:
        with self.assertRaises(PrivateFormatError):
            parse_position_risk({"symbol": SYMBOL}, symbol=SYMBOL, receive_ts=1, process_ts=1)

    def test_invalid_values_fail_closed(self) -> None:
        for overrides in (
            {"mark_price": "0"},
            {"mark_price": "abc"},
            {"leverage": 0},
            {"leverage": 1.5},
            {"liquidation_price": "-1"},
            {"position_amt": "abc"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(PrivateFormatError):
                    parse_position_risk(
                        position_risk_payload(**overrides), symbol=SYMBOL, receive_ts=1, process_ts=1  # type: ignore[arg-type]
                    )

    def test_flat_position_allows_zero_prices(self) -> None:
        position = parse_position_risk(
            position_risk_payload(position_amt="0", entry_price="0", liquidation_price="0", unrealized_profit="0"),
            symbol=SYMBOL,
            receive_ts=1,
            process_ts=1,
        )

        self.assertTrue(position.is_flat)
        self.assertEqual(position.entry_price, 0.0)
        self.assertEqual(position.liquidation_price, 0.0)


if __name__ == "__main__":
    unittest.main()
