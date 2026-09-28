"""P0001.9.1 单元测试：aggTrade → TradePayload（SC-5 / SC-6 / SC-11）。"""

from __future__ import annotations

import unittest

from market.events.payloads import AggressorSide
from market.events.types import EventType

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.trades import (
    AggTradeDeduplicator,
    aggressor_for_buyer_maker,
    parse_agg_trade,
)

RECEIVE_TS = 1_700_000_000_500
PROCESS_TS = 1_700_000_000_600


def _raw(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "e": "aggTrade",
        "E": 1_700_000_000_000,
        "a": 7,
        "s": "BTCUSDT",
        "p": "100.5",
        "q": "2.5",
        "f": 5,
        "l": 6,
        "T": 1_700_000_000_100,
        "m": True,
    }
    payload.update(overrides)
    return payload


class AggressorMappingTest(unittest.TestCase):
    def test_buyer_is_maker_means_sell_aggressor(self) -> None:
        """SC-5：`m = true`（买方是 maker）⇒ 主动方是 SELL，语义不得反转。"""
        self.assertIs(aggressor_for_buyer_maker(True), AggressorSide.SELL)

    def test_buyer_is_taker_means_buy_aggressor(self) -> None:
        self.assertIs(aggressor_for_buyer_maker(False), AggressorSide.BUY)

    def test_non_boolean_is_rejected(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            aggressor_for_buyer_maker(1)  # type: ignore[arg-type]

    def test_parse_maps_the_field(self) -> None:
        sell = parse_agg_trade(_raw(m=True), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS).payload
        buy = parse_agg_trade(_raw(m=False), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS).payload

        self.assertIs(sell.aggressor, AggressorSide.SELL)
        self.assertIs(buy.aggressor, AggressorSide.BUY)


class ParseAggTradeTest(unittest.TestCase):
    def test_event_shape_and_timestamps(self) -> None:
        """SC-11：交易所时间与本地接收时间都保留，且 exchange_ts 用交易时间 T。"""
        event = parse_agg_trade(_raw(), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)

        self.assertIs(event.event_type, EventType.TRADE)
        self.assertIsNone(event.sequence)
        self.assertEqual(event.exchange_ts, 1_700_000_000_100)
        self.assertEqual(event.receive_ts, RECEIVE_TS)
        self.assertEqual(event.process_ts, PROCESS_TS)
        self.assertEqual(event.symbol, "BTCUSDT")

    def test_payload_fields(self) -> None:
        payload = parse_agg_trade(_raw(), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS).payload

        self.assertEqual(payload.aggregate_trade_id, 7)
        self.assertEqual(payload.price, 100.5)
        self.assertEqual(payload.quantity, 2.5)

    def test_symbol_mismatch_is_rejected(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_agg_trade(_raw(s="ETHUSDT"), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS, symbol="BTCUSDT")

    def test_wrong_event_name_is_rejected(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_agg_trade(_raw(e="trade"), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)

    def test_malformed_fields_are_rejected(self) -> None:
        for overrides in (
            {"a": "7"},
            {"p": "abc"},
            {"q": "0"},
            {"m": "true"},
            {"T": "1"},
            {"T": -1},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(MarketDataFormatError):
                    parse_agg_trade(_raw(**overrides), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)

    def test_missing_field_is_rejected(self) -> None:
        raw = _raw()
        del raw["T"]

        with self.assertRaises(MarketDataFormatError):
            parse_agg_trade(raw, receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)


class DeduplicationTest(unittest.TestCase):
    def test_sc6_monotonic_watermark_drops_duplicates(self) -> None:
        dedup = AggTradeDeduplicator()

        self.assertTrue(dedup.accept(symbol="BTCUSDT", aggregate_trade_id=5))
        self.assertFalse(dedup.accept(symbol="BTCUSDT", aggregate_trade_id=5))  # 重复
        self.assertFalse(dedup.accept(symbol="BTCUSDT", aggregate_trade_id=4))  # 乱序旧值
        self.assertTrue(dedup.accept(symbol="BTCUSDT", aggregate_trade_id=6))
        self.assertEqual(dedup.dropped, 2)
        self.assertEqual(dedup.watermark("BTCUSDT"), 6)

    def test_watermarks_are_per_symbol(self) -> None:
        dedup = AggTradeDeduplicator()

        self.assertTrue(dedup.accept(symbol="BTCUSDT", aggregate_trade_id=100))
        self.assertTrue(dedup.accept(symbol="ETHUSDT", aggregate_trade_id=1))

    def test_invalid_inputs_are_rejected(self) -> None:
        dedup = AggTradeDeduplicator()

        with self.assertRaises(MarketDataFormatError):
            dedup.accept(symbol="", aggregate_trade_id=1)
        with self.assertRaises(MarketDataFormatError):
            dedup.accept(symbol="BTCUSDT", aggregate_trade_id=True)


if __name__ == "__main__":
    unittest.main()
