"""Candle aggregation（chart workbench 后端只读数据源）。"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from product.candles import (CANDLE_INTERVALS, CandleError, DEFAULT_CANDLE_LIMIT, MAX_CANDLE_LIMIT,
                             SOURCE_MID, SOURCE_TRADES, aggregate_candles)


def trade(ts: int, price: float, quantity: float) -> object:
    return SimpleNamespace(ts=ts, price=price, quantity=quantity)


def snapshot(ts: int, bid: float, ask: float) -> object:
    return SimpleNamespace(ts=ts, bids=((bid, 1.0),), asks=((ask, 1.0),))


class TradeAggregationTest(unittest.TestCase):
    def test_ohlcv_from_trades_in_one_bucket(self) -> None:
        series = aggregate_candles(trades=[trade(60_000, 100.0, 2.0), trade(60_500, 105.0, 1.0),
                                           trade(60_900, 95.0, 3.0)],
                                   snapshots=[], interval="1m", limit=10)
        self.assertEqual(series.source, SOURCE_TRADES)
        self.assertEqual(len(series.candles), 1)
        candle = series.candles[0]
        self.assertEqual((candle.open, candle.high, candle.low, candle.close), (100.0, 105.0, 95.0, 95.0))
        self.assertEqual(candle.volume, 6.0)
        self.assertEqual(candle.ts, 60_000)

    def test_buckets_align_to_interval(self) -> None:
        series = aggregate_candles(trades=[trade(1, 1.0, 1.0), trade(299_999, 2.0, 1.0),
                                           trade(300_000, 3.0, 1.0)],
                                   snapshots=[], interval="5m", limit=10)
        self.assertEqual([c.ts for c in series.candles], [0, 300_000])

    def test_limit_is_bounded_and_truncated_is_explicit(self) -> None:
        trades = [trade(index * 60_000, 100.0 + index, 1.0) for index in range(5)]
        series = aggregate_candles(trades=trades, snapshots=[], interval="1m", limit=2)
        self.assertEqual(len(series.candles), 2)
        self.assertTrue(series.truncated)
        self.assertEqual(series.total_buckets, 5)
        self.assertEqual(series.candles[-1].close, 104.0)

    def test_mid_source_when_no_trades(self) -> None:
        series = aggregate_candles(trades=[], snapshots=[snapshot(1_000, 99.0, 101.0),
                                                        snapshot(1_500, 100.0, 102.0)],
                                   interval="1m", limit=10)
        self.assertEqual(series.source, SOURCE_MID)
        candle = series.candles[0]
        self.assertEqual((candle.open, candle.high, candle.low, candle.close), (100.0, 101.0, 100.0, 101.0))
        self.assertEqual(candle.volume, 0.0)              # 不伪造成交量

    def test_trades_take_precedence_over_mid_in_same_bucket(self) -> None:
        series = aggregate_candles(trades=[trade(61_000, 200.0, 1.0)],
                                   snapshots=[snapshot(61_500, 100.0, 102.0)],
                                   interval="1m", limit=10)
        self.assertEqual(series.candles[0].source, SOURCE_TRADES)
        self.assertEqual(series.candles[0].close, 200.0)

    def test_empty_facts_yield_empty_series(self) -> None:
        series = aggregate_candles(trades=[], snapshots=[], interval="1m", limit=10)
        self.assertEqual(series.candles, ())
        self.assertFalse(series.truncated)

    def test_invalid_params(self) -> None:
        for interval in ("2m", "1d", ""):
            with self.subTest(interval=interval):
                with self.assertRaises(CandleError):
                    aggregate_candles(trades=[], snapshots=[], interval=interval)
        for limit in (0, -1, MAX_CANDLE_LIMIT + 1, True):
            with self.subTest(limit=limit):
                with self.assertRaises(CandleError):
                    aggregate_candles(trades=[], snapshots=[], interval="1m", limit=limit)

    def test_intervals_and_defaults(self) -> None:
        self.assertEqual(tuple(CANDLE_INTERVALS), ("1m", "5m", "15m", "1h"))
        self.assertLessEqual(DEFAULT_CANDLE_LIMIT, MAX_CANDLE_LIMIT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
