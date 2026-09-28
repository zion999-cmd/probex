"""P0001.9.1 单元测试：exchangeInfo → TradingRules（SC-8）。"""

from __future__ import annotations

import unittest

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.exchange_info import parse_exchange_info
from tests.live_support import exchange_info_payload


class TradingRulesTest(unittest.TestCase):
    def test_rules_come_from_filters(self) -> None:
        rules = parse_exchange_info(
            exchange_info_payload(tick_size="0.10", step_size="0.001", min_notional="5"), symbol="BTCUSDT"
        )

        self.assertEqual(rules.symbol, "BTCUSDT")
        self.assertEqual(rules.status, "TRADING")
        self.assertAlmostEqual(rules.tick_size, 0.10)
        self.assertAlmostEqual(rules.step_size, 0.001)
        self.assertAlmostEqual(rules.min_notional, 5.0)
        self.assertAlmostEqual(rules.min_qty, 0.001)
        self.assertAlmostEqual(rules.max_qty, 1000.0)
        self.assertTrue(rules.is_trading)

    def test_sc8_precision_fields_are_not_used(self) -> None:
        """SC-8：即使 precision 字段与 filter 冲突，也只信 filter。"""
        payload = exchange_info_payload(tick_size="0.5", step_size="0.25")
        payload["symbols"][0]["pricePrecision"] = 8
        payload["symbols"][0]["quantityPrecision"] = 8

        rules = parse_exchange_info(payload, symbol="BTCUSDT")

        self.assertAlmostEqual(rules.tick_size, 0.5)
        self.assertAlmostEqual(rules.step_size, 0.25)

    def test_missing_filter_fails_closed(self) -> None:
        payload = exchange_info_payload()
        payload["symbols"][0]["filters"] = [
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"}
        ]

        with self.assertRaises(MarketDataFormatError):
            parse_exchange_info(payload, symbol="BTCUSDT")

    def test_unknown_symbol_fails_closed(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_exchange_info(exchange_info_payload(), symbol="ETHUSDT")

    def test_invalid_values_are_rejected(self) -> None:
        for tick in ("0", "-1", "abc"):
            with self.subTest(tick=tick):
                with self.assertRaises(MarketDataFormatError):
                    parse_exchange_info(exchange_info_payload(tick_size=tick), symbol="BTCUSDT")

    def test_broken_filters_are_rejected(self) -> None:
        payload = exchange_info_payload()
        payload["symbols"][0]["filters"] = "not-an-array"

        with self.assertRaises(MarketDataFormatError):
            parse_exchange_info(payload, symbol="BTCUSDT")

    def test_non_trading_status_is_reported(self) -> None:
        rules = parse_exchange_info(exchange_info_payload(status="BREAK"), symbol="BTCUSDT")

        self.assertFalse(rules.is_trading)
        self.assertEqual(rules.status, "BREAK")


if __name__ == "__main__":
    unittest.main()
