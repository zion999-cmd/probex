"""P0001.9.1 单元测试：stream 名称 / tier 归属 / 信封解析。"""

from __future__ import annotations

import json
import unittest

from connectors.binance.market_data.endpoints import StreamTier
from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.streams import (
    StreamKind,
    agg_trade_stream,
    depth_stream,
    mark_price_stream,
    parse_message,
    stream_matches,
    streams_for,
    subscribe_message,
)


class StreamNameTest(unittest.TestCase):
    def test_stream_names_are_lowercased(self) -> None:
        self.assertEqual(depth_stream("BTCUSDT"), "btcusdt@depth@100ms")
        self.assertEqual(agg_trade_stream("BTCUSDT"), "btcusdt@aggTrade")
        self.assertEqual(mark_price_stream("BTCUSDT"), "btcusdt@markPrice@1s")

    def test_tier_mapping_after_the_2026_routing_change(self) -> None:
        self.assertIs(StreamKind.DEPTH.tier, StreamTier.PUBLIC)
        self.assertIs(StreamKind.AGG_TRADE.tier, StreamTier.MARKET)
        self.assertIs(StreamKind.MARK_PRICE.tier, StreamTier.MARKET)

    def test_streams_for_is_ordered(self) -> None:
        streams = streams_for("BTCUSDT", (StreamKind.DEPTH, StreamKind.AGG_TRADE, StreamKind.MARK_PRICE))

        self.assertEqual(streams, ("btcusdt@depth@100ms", "btcusdt@aggTrade", "btcusdt@markPrice@1s"))

    def test_url_construction(self) -> None:
        self.assertEqual(
            StreamTier.PUBLIC.combined_stream_url(("btcusdt@depth@100ms",)),
            "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms",
        )
        self.assertEqual(
            StreamTier.MARKET.single_stream_url("btcusdt@aggTrade"),
            "wss://fstream.binance.com/market/ws/btcusdt@aggTrade",
        )
        self.assertEqual(StreamTier.PUBLIC.with_host("ws://127.0.0.1:9"), "ws://127.0.0.1:9/public")

    def test_empty_symbol_or_streams_are_rejected(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            depth_stream("")
        with self.assertRaises(ValueError):
            subscribe_message((), request_id=1)
        with self.assertRaises(ValueError):
            StreamTier.PUBLIC.combined_stream_url(())


class SubscribeTest(unittest.TestCase):
    def test_subscribe_message_shape(self) -> None:
        text = subscribe_message(("btcusdt@aggTrade", "btcusdt@markPrice@1s"), request_id=7)

        self.assertEqual(
            json.loads(text), {"method": "SUBSCRIBE", "params": ["btcusdt@aggTrade", "btcusdt@markPrice@1s"], "id": 7}
        )

    def test_request_id_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            subscribe_message(("a@b",), request_id=0)


class MessageTest(unittest.TestCase):
    def test_combined_envelope(self) -> None:
        message = parse_message('{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","a":1}}')

        self.assertEqual(message.stream, "btcusdt@aggTrade")
        self.assertEqual(message.data["e"], "aggTrade")

    def test_single_stream_envelope(self) -> None:
        message = parse_message('{"e":"depthUpdate","U":1,"u":2}')

        self.assertIsNone(message.stream)
        self.assertEqual(message.data["U"], 1)

    def test_ack_is_not_a_business_message(self) -> None:
        self.assertIsNone(parse_message('{"result":null,"id":1}'))
        self.assertIsNone(parse_message('{"error":{"code":-1,"msg":"bad"},"id":1}'))

    def test_malformed_messages_raise(self) -> None:
        for text in ("", "not json", "[1,2]", '{"foo":1}', '{"data":1}', '{"data":{},"stream":5}'):
            with self.subTest(text=text):
                with self.assertRaises(MarketDataFormatError):
                    parse_message(text)

    def test_stream_matches(self) -> None:
        message = parse_message('{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade"}}')

        self.assertTrue(stream_matches(message, "btcusdt@aggTrade"))
        self.assertFalse(stream_matches(message, "btcusdt@depth@100ms"))
        self.assertTrue(stream_matches(parse_message('{"e":"aggTrade"}'), "anything"))


if __name__ == "__main__":
    unittest.main()
