"""P0001.9.1 单元测试：mark price 作为独立事实（SC-7 / SC-11）。"""

from __future__ import annotations

import unittest

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.mark import MarkPriceObservation, parse_mark_price
from connectors.binance.market_data.trades import parse_agg_trade


def _raw(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"e": "markPriceUpdate", "E": 1_700_000_000_123, "s": "BTCUSDT", "p": "100.75"}
    payload.update(overrides)
    return payload


class MarkPriceTest(unittest.TestCase):
    def test_observation_shape(self) -> None:
        observation = parse_mark_price(_raw(), receive_ts=10, process_ts=11)

        self.assertEqual(observation.symbol, "BTCUSDT")
        self.assertEqual(observation.price, 100.75)
        self.assertEqual(observation.exchange_ts, 1_700_000_000_123)
        self.assertEqual((observation.receive_ts, observation.process_ts), (10, 11))

    def test_missing_or_invalid_price_is_rejected(self) -> None:
        for overrides in ({"p": None}, {"p": "abc"}, {"p": "0"}, {"p": "-1"}, {}):
            raw = _raw(**overrides)
            if not overrides:
                del raw["p"]
            with self.subTest(overrides=overrides):
                with self.assertRaises(MarketDataFormatError):
                    parse_mark_price(raw, receive_ts=1, process_ts=2)

    def test_wrong_event_name_is_rejected(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_mark_price(_raw(e="aggTrade"), receive_ts=1, process_ts=2)

    def test_symbol_mismatch_is_rejected(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_mark_price(_raw(s="ETHUSDT"), receive_ts=1, process_ts=2, symbol="BTCUSDT")

    def test_sc7_trade_events_never_produce_a_mark_observation(self) -> None:
        """SC-7：mark 是独立事实，绝不能由 last trade 冒充。"""
        trade = parse_agg_trade(
            {"e": "aggTrade", "E": 1, "a": 1, "s": "BTCUSDT", "p": "99.0", "q": "1", "T": 1, "m": False},
            receive_ts=1,
            process_ts=2,
        )

        self.assertNotIsInstance(trade, MarkPriceObservation)
        self.assertNotIsInstance(trade.payload, MarkPriceObservation)

    def test_observation_validates_its_inputs(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            MarkPriceObservation(venue="binance", symbol="X", price=1.0, exchange_ts=1, receive_ts=1, process_ts=1)  # type: ignore[arg-type]
        with self.assertRaises(MarketDataFormatError):
            MarkPriceObservation(venue=__import__("market.events.types", fromlist=["Venue"]).Venue.BINANCE, symbol="", price=1.0, exchange_ts=1, receive_ts=1, process_ts=1)
        with self.assertRaises(MarketDataFormatError):
            MarkPriceObservation(
                venue=__import__("market.events.types", fromlist=["Venue"]).Venue.BINANCE,
                symbol="X",
                price=0.0,
                exchange_ts=1,
                receive_ts=1,
                process_ts=1,
            )


if __name__ == "__main__":
    unittest.main()
