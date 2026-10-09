"""P0001.17 授权补齐的单元验收：成交域事实 / 服务端指标 / run 级事实持久化。

覆盖：
- `TradeFeatureAccumulator`（真实成交 ⇒ vwap/cvd/笔数/强度；窗口过期 ⇒ None；空窗 ⇒ None）
- `wilder_atr`（标准定义；不足周期 ⇒ None）
- `FeatureEngine` 消费 TRADE（成交事实真实；盘口不被 trade 污染）
- `RunRegistry` run 级事实（append/load；缺失 ⇒ 明确原因）
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from market.events.payloads import AggressorSide, TradePayload
from market.events.types import EventType
from market.features.engine import FeatureEngine
from market.events.types import Venue
from product.candles import Candle
from product.indicators import IndicatorError, wilder_atr
from storage.run_registry import JsonRunRegistry, RunRegistryError
from tests.ui.market_fixture import build_market_events


class TradeFeatureAccumulatorTest(unittest.TestCase):
    def accumulator(self, *, window_ms: int = 300_000):
        from market.features.trade import TradeFeatureAccumulator

        return TradeFeatureAccumulator(window_ms=window_ms)

    def test_vwap_and_cvd_from_real_prints(self) -> None:
        acc = self.accumulator()
        acc.on_trade(TradePayload(aggregate_trade_id=1, price=100.0, quantity=2.0,
                                  aggressor=AggressorSide.BUY), timestamp=1_000)
        acc.on_trade(TradePayload(aggregate_trade_id=2, price=110.0, quantity=1.0,
                                  aggressor=AggressorSide.SELL), timestamp=2_000)

        snapshot = acc.snapshot(at=2_000)
        self.assertTrue(snapshot.trade_stream_available)
        self.assertAlmostEqual(snapshot.vwap, (100.0 * 2 + 110.0 * 1) / 3)
        self.assertAlmostEqual(snapshot.buy_aggressive_volume, 2.0)
        self.assertAlmostEqual(snapshot.sell_aggressive_volume, 1.0)
        self.assertAlmostEqual(snapshot.signed_volume, 1.0)
        self.assertAlmostEqual(snapshot.cvd, 1.0)
        self.assertEqual(snapshot.trade_count, 2)
        self.assertGreater(snapshot.trade_intensity, 0.0)

    def test_expired_prints_leave_unknown_not_zero(self) -> None:
        acc = self.accumulator(window_ms=1_000)
        acc.on_trade(TradePayload(aggregate_trade_id=1, price=100.0, quantity=1.0,
                                  aggressor=AggressorSide.BUY), timestamp=1_000)

        snapshot = acc.snapshot(at=600_000)                 # 窗口已过期
        self.assertIsNone(snapshot.vwap)
        self.assertIsNone(snapshot.trade_count)
        self.assertIsNone(snapshot.cvd)
        self.assertTrue(snapshot.trade_stream_available)    # 曾观测到成交（与"窗口内有量"不同）

    def test_empty_window_reports_no_stream(self) -> None:
        snapshot = self.accumulator().snapshot(at=1_000)
        self.assertFalse(snapshot.trade_stream_available)
        self.assertIsNone(snapshot.vwap)

    def test_window_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            self.accumulator(window_ms=0)


class WilderAtrTest(unittest.TestCase):
    def candles(self, count: int):
        return tuple(Candle(ts=index * 60_000, open=10.0 + index, high=12.0 + index,
                            low=9.0 + index, close=11.0 + index, volume=1.0, source="trades")
                     for index in range(count))

    def test_atr_is_unknown_until_the_period_is_reached(self) -> None:
        points = wilder_atr(self.candles(5), period=3)
        self.assertEqual([p.atr for p in points[:2]], [None, None])
        self.assertIsNotNone(points[2].atr)

    def test_atr_uses_wilder_smoothing_after_the_seed(self) -> None:
        points = wilder_atr(self.candles(4), period=3)
        seed = points[2].atr
        self.assertIsNotNone(seed)
        self.assertAlmostEqual(seed, 3.0)                     # 恒定 TR=3 ⇒ 种子 = 3
        self.assertAlmostEqual(points[3].atr, (seed * 2 + 3.0) / 3)

    def test_period_must_be_positive(self) -> None:
        with self.assertRaises(IndicatorError):
            wilder_atr(self.candles(3), period=0)


class EngineTradeRoutingTest(unittest.TestCase):
    def test_engine_consumes_trades_without_touching_the_book(self) -> None:
        engine = FeatureEngine(Venue.BINANCE, "BTCUSDT")
        last_book_state = None
        last_trade_state = None
        book_before_trade = None
        for event in build_market_events(hours=1)[:80]:
            if event.event_type in (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA):
                last_book_state = engine.on_market_event(event)
            elif event.event_type is EventType.TRADE:
                book_before_trade = last_book_state        # 该成交**之前**的盘口事实
                last_trade_state = engine.on_market_event(event)

        self.assertIsNotNone(last_trade_state)
        self.assertTrue(last_trade_state.trade.trade_stream_available)
        self.assertIsNotNone(last_trade_state.trade.vwap)
        self.assertGreaterEqual(last_trade_state.trade.trade_count or 0, 1)
        # 成交事件不改变盘口事实：沿用**该成交之前**的已知盘口状态
        self.assertIsNotNone(book_before_trade)
        self.assertEqual(last_trade_state.price.mid, book_before_trade.price.mid)
        self.assertEqual(last_trade_state.depth.bid_depth_1, book_before_trade.depth.bid_depth_1)


class RunFactsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="probex-runfacts-"))
        self.registry = JsonRunRegistry(self.tmp)

    def test_append_and_load_market_and_fact_entries(self) -> None:
        written = self.registry.append_run_facts("run-1", "market",
                                                [{"ts": 1, "mid": 100.0}, {"ts": 2, "mid": 101.0}])
        self.assertEqual(written, 2)
        self.registry.append_run_facts("run-1", "facts", [{"kind": "order", "ts": 2,
                                                          "client_order_id": "c-1"}])

        points, unavailable = self.registry.load_run_facts("run-1", "market")
        self.assertIsNone(unavailable)
        self.assertEqual([p["ts"] for p in points], [1, 2])
        facts, _ = self.registry.load_run_facts("run-1", "facts")
        self.assertEqual(facts[0]["client_order_id"], "c-1")

    def test_missing_run_facts_report_a_reason_instead_of_empty_success(self) -> None:
        points, unavailable = self.registry.load_run_facts("missing-run", "market")
        self.assertEqual(points, ())
        self.assertIn("no market facts recorded", str(unavailable))

    def test_limit_returns_the_most_recent_entries(self) -> None:
        self.registry.append_run_facts("run-2", "market", [{"ts": ts} for ts in range(10)])
        points, _ = self.registry.load_run_facts("run-2", "market", limit=3)
        self.assertEqual([p["ts"] for p in points], [7, 8, 9])

    def test_unknown_fact_kind_is_refused(self) -> None:
        with self.assertRaises(RunRegistryError):
            self.registry.append_run_facts("run-3", "bogus", [{"ts": 1}])


if __name__ == "__main__":
    unittest.main()
