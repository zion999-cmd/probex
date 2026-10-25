"""Bar 适配器双模式成交假设单元测试（D-056 bar 授权）。

时序与真实 driver 一致：先 on_bar（收盘）→ submit（决策挂单）→ 后续 on_bar 判定成交。
"""

from __future__ import annotations

import unittest

from execution.events import FillReceived, OrderRejected
from execution.simulation.bar_adapter import (
    CLOSE_MARKET,
    LIMIT_CONSERVATIVE,
    BarAdapterError,
    BarExecutionAdapter,
)
from execution.simulation.fees import FeeSchedule
from execution.simulation.slippage import FixedBpsSlippage
from execution.types import Order, OrderStatus
from market.events.payloads import BarPayload
from market.events.types import Venue
from portfolio.types import Side

FEES = FeeSchedule(maker_fee_rate=0.0002, taker_fee_rate=0.0005, fee_asset="USDT")
BAR0 = (0, 1_000, 100.0, 101.0, 99.0, 100.0)
BAR1 = (1_000, 2_000, 100.0, 101.0, 99.0, 100.0)
BAR2 = (2_000, 3_000, 102.0, 103.0, 101.0, 102.5)
BAR3 = (3_000, 4_000, 98.0, 99.0, 97.0, 98.5)


def _bar(spec, volume: float = 10.0) -> BarPayload:
    open_ts, close_ts, open_, high, low, close = spec
    return BarPayload(open_ts=open_ts, close_ts=close_ts,
                         interval_ms=close_ts - open_ts,
                         open=open_, high=high, low=low, close=close, volume=volume)


def _order(cid: str, side: Side, price: float, *, post_only: bool) -> Order:
    return Order(
        client_order_id=cid, venue=Venue.BINANCE, symbol="BTCUSDT", side=side,
        price=price, quantity=1.0, status=OrderStatus.PENDING_CREATE,
        created_at=0, updated_at=0, order_type="limit", post_only=post_only)


def _submit_limit(adapter: BarExecutionAdapter, cid: str, side: Side, price: float) -> None:
    events = adapter.submit(_order(cid, side, price, post_only=True))
    assert not any(isinstance(e, OrderRejected) for e in events), events


class LimitConservativeAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = BarExecutionAdapter(
            symbol="BTCUSDT", mode=LIMIT_CONSERVATIVE, fee_schedule=FEES)
        self.adapter.on_bar(_bar(BAR0))  # 决策根

    def test_buy_fills_next_bar_when_low_touches_limit(self) -> None:
        _submit_limit(self.adapter, "b1", Side.BUY, 99.0)
        self.adapter.on_bar(_bar(BAR1))
        fills = [e for e in self.adapter.poll() if isinstance(e, FillReceived)]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].price, 99.0)
        self.assertTrue(fills[0].is_maker)
        self.assertAlmostEqual(fills[0].fee, 99.0 * 1.0 * 0.0002)

    def test_sell_fills_next_bar_when_high_touches_limit(self) -> None:
        _submit_limit(self.adapter, "s1", Side.SELL, 101.0)
        self.adapter.on_bar(_bar(BAR1))
        fills = [e for e in self.adapter.poll() if isinstance(e, FillReceived)]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].price, 101.0)

    def test_no_fill_when_untouched_then_fills_later(self) -> None:
        _submit_limit(self.adapter, "b2", Side.BUY, 90.0)
        self.adapter.on_bar(_bar(BAR1))  # low 99 未触 90
        self.assertEqual(self.adapter.poll(), ())
        self.adapter.on_bar(_bar((2_000, 3_000, 100.0, 100.0, 90.0, 95.0)))
        self.assertEqual(len([e for e in self.adapter.poll() if isinstance(e, FillReceived)]), 1)

    def test_bar_spanning_limit_fills_at_fixed_price_regardless_of_path(self) -> None:
        # 盘内既到 high=102 又到 low=98：限价固定 100，成交价格不受路径影响
        _submit_limit(self.adapter, "b3", Side.BUY, 100.0)
        self.adapter.on_bar(_bar((1_000, 2_000, 100.0, 102.0, 98.0, 101.0)))
        fill = [e for e in self.adapter.poll() if isinstance(e, FillReceived)][0]
        self.assertEqual(fill.price, 100.0)

    def test_same_bar_decision_does_not_look_into_current_bar(self) -> None:
        # on_bar 之后挂的单不能在当根成交（无前视）
        self.adapter.on_bar(_bar(BAR1))
        _submit_limit(self.adapter, "b4", Side.BUY, 99.0)
        self.assertEqual(self.adapter.poll(), ())
        self.adapter.on_bar(_bar((2_000, 3_000, 99.0, 100.0, 99.0, 99.5)))
        self.assertEqual(len([e for e in self.adapter.poll() if isinstance(e, FillReceived)]), 1)

    def test_taker_order_rejected_in_limit_mode(self) -> None:
        events = self.adapter.submit(_order("t1", Side.BUY, 100.0, post_only=False))
        self.assertIsInstance(events[0], OrderRejected)
        self.assertEqual(events[0].reason, "TAKER_NOT_SUPPORTED")


class CloseMarketAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = BarExecutionAdapter(
            symbol="BTCUSDT", mode=CLOSE_MARKET, fee_schedule=FEES,
            slippage=FixedBpsSlippage(10.0))
        self.adapter.on_bar(_bar(BAR0))  # 决策根

    def test_market_buy_fills_next_bar_open_plus_slippage_taker_fee(self) -> None:
        rejected = self.adapter.submit(_order("m1", Side.BUY, 100.0, post_only=False))
        self.assertFalse(any(isinstance(e, OrderRejected) for e in rejected))
        # 下一根 open 成交：102 × 1.001（决策当根已过，不回填）
        self.adapter.on_bar(_bar(BAR2))
        fills = [e for e in self.adapter.poll() if isinstance(e, FillReceived)]
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].price, 102.0 * 1.001)
        self.assertFalse(fills[0].is_maker)
        self.assertAlmostEqual(fills[0].fee, fills[0].price * 1.0 * 0.0005)

    def test_market_sell_fills_next_open_minus_slippage(self) -> None:
        self.adapter.submit(_order("m2", Side.SELL, 100.0, post_only=False))
        self.adapter.on_bar(_bar(BAR3))
        fill = [e for e in self.adapter.poll() if isinstance(e, FillReceived)][0]
        self.assertAlmostEqual(fill.price, 98.0 * 0.999)

    def test_post_only_rejected_in_close_market_mode(self) -> None:
        events = self.adapter.submit(_order("p1", Side.BUY, 100.0, post_only=True))
        self.assertEqual(events[0].reason, "POST_ONLY_UNSUPPORTED_IN_CLOSE_MARKET")

    def test_construction_requires_slippage_and_taker_fee(self) -> None:
        with self.assertRaises(BarAdapterError):
            BarExecutionAdapter(symbol="BTCUSDT", mode=CLOSE_MARKET, fee_schedule=FEES)
        maker_only_fee = FeeSchedule(maker_fee_rate=0.0002, fee_asset="USDT")
        with self.assertRaises(BarAdapterError):
            BarExecutionAdapter(symbol="BTCUSDT", mode=CLOSE_MARKET,
                                  fee_schedule=maker_only_fee,
                                  slippage=FixedBpsSlippage(10.0))

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(BarAdapterError):
            BarExecutionAdapter(symbol="BTCUSDT", mode="intraday", fee_schedule=FEES)


if __name__ == "__main__":
    unittest.main()
