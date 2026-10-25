"""固定 bps 滑点 / taker 模拟单元测试（D-055 批次4，人类授权）。

- FixedBpsSlippage：买高卖低的确定性调整；
- FeeSchedule：taker 费率显式可选，未配置不可用；
- SimulatedVenue：默认 maker-only；配置后非 post-only 订单立即吃单（taker 费率/TAKER_CROSS），
  盘口未知 fail-closed，post-only 挂单行为不变。
"""

from __future__ import annotations

import unittest

from execution.events import FillReceived, OrderRejected, OrderAccepted
from execution.simulation.fees import FeeSchedule
from execution.simulation.latency import LatencyModel
from execution.simulation.slippage import FixedBpsSlippage, SlippageError
from execution.simulation.types import (
    FillReason,
    Liquidity,
    SimulatedRejectReason,
    SimulationError,
)
from execution.simulation.venue import SimulatedVenue
from execution.types import Order, OrderStatus
from market.events.types import Venue
from portfolio.types import Side
from tests.sim_support import book_snapshot, ts

FEE = FeeSchedule(maker_fee_rate=0.0002, taker_fee_rate=0.0005, fee_asset="USDT")


def _venue(*, slippage=None, fee_schedule=FEE) -> SimulatedVenue:
    return SimulatedVenue(
        symbol="BTCUSDT",
        latency=LatencyModel(submit_latency_ms=0, cancel_latency_ms=0),
        fee_schedule=fee_schedule,
        slippage=slippage)


def _taker_order(cid: str, side: Side, *, price: float = 100.0, qty: float = 1.0) -> Order:
    return Order(
        client_order_id=cid, venue=Venue.BINANCE, symbol="BTCUSDT", side=side,
        price=price, quantity=qty, status=OrderStatus.PENDING_CREATE,
        created_at=ts(300_000), updated_at=ts(300_000),
        order_type="limit", post_only=False)


class FixedBpsSlippageTest(unittest.TestCase):
    def test_buy_pays_above_ask_sell_receives_below_bid(self) -> None:
        model = FixedBpsSlippage(10.0)  # 10 bps
        self.assertAlmostEqual(model.exec_price(side=Side.BUY, reference_price=100.0), 100.1)
        self.assertAlmostEqual(model.exec_price(side=Side.SELL, reference_price=100.0), 99.9)

    def test_zero_bps_uses_reference(self) -> None:
        model = FixedBpsSlippage(0.0)
        self.assertEqual(model.exec_price(side=Side.BUY, reference_price=100.0), 100.0)

    def test_invalid_config_rejected(self) -> None:
        for bad in (-1.0, True, float("nan"), float("inf")):
            with self.assertRaises(SlippageError):
                FixedBpsSlippage(bad)  # type: ignore[arg-type]
        model = FixedBpsSlippage(1.0)
        for bad in (0.0, -1.0, None):
            with self.assertRaises(SlippageError):
                model.exec_price(side=Side.BUY, reference_price=bad)  # type: ignore[arg-type]


class TakerFeeScheduleTest(unittest.TestCase):
    def test_taker_fee_optional_and_explicit(self) -> None:
        schedule = FeeSchedule(maker_fee_rate=0.0002, fee_asset="USDT")
        with self.assertRaises(SimulationError):
            schedule.taker_fee(price=100.0, quantity=1.0)
        enabled = FeeSchedule(maker_fee_rate=0.0002, taker_fee_rate=0.0005,
                                 fee_asset="USDT")
        self.assertAlmostEqual(enabled.taker_fee(price=100.0, quantity=2.0), 0.1)


class SimulatedVenueTakerTest(unittest.TestCase):
    def test_non_post_order_rejected_when_venue_maker_only(self) -> None:
        venue = _venue(slippage=None)
        venue.on_market_event(book_snapshot())
        events = venue.submit(_taker_order("t1", Side.BUY))
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], OrderRejected)
        self.assertEqual(events[0].reason, SimulatedRejectReason.TAKER_NOT_SUPPORTED.value)

    def test_non_post_order_rejected_without_taker_fee(self) -> None:
        maker_only_fee = FeeSchedule(maker_fee_rate=0.0002, fee_asset="USDT")
        venue = _venue(slippage=FixedBpsSlippage(10.0), fee_schedule=maker_only_fee)
        venue.on_market_event(book_snapshot())
        events = venue.submit(_taker_order("t1", Side.BUY))
        self.assertEqual(events[0].reason, SimulatedRejectReason.TAKER_NOT_SUPPORTED.value)

    def test_taker_buy_fills_immediately_at_ask_plus_slippage_with_taker_fee(self) -> None:
        venue = _venue(slippage=FixedBpsSlippage(10.0))
        venue.on_market_event(book_snapshot())
        accepted = venue.submit(_taker_order("t1", Side.BUY))
        self.assertIsInstance(accepted[0], OrderAccepted)
        polled = venue.poll()
        fill = next(e for e in polled if isinstance(e, FillReceived))
        self.assertAlmostEqual(fill.price, 101.0 * 1.001)  # best ask 101 + 10 bps
        self.assertFalse(fill.is_maker)
        self.assertAlmostEqual(fill.fee, fill.price * 1.0 * 0.0005)
        record = venue.fills[0]
        self.assertIs(record.liquidity, Liquidity.TAKER)
        self.assertIs(record.fill_reason, FillReason.TAKER_CROSS)
        self.assertIsNone(record.aggregate_trade_id)
        self.assertEqual(venue.order_view("t1").status, OrderStatus.FILLED)

    def test_taker_sell_fills_at_bid_minus_slippage(self) -> None:
        venue = _venue(slippage=FixedBpsSlippage(10.0))
        venue.on_market_event(book_snapshot())
        venue.submit(_taker_order("t2", Side.SELL))
        fill = next(e for e in venue.poll() if isinstance(e, FillReceived))
        self.assertAlmostEqual(fill.price, 100.0 * 0.999)  # best bid 100 − 10 bps

    def test_taker_without_book_is_rejected_not_guessed(self) -> None:
        venue = _venue(slippage=FixedBpsSlippage(10.0))
        # 未喂任何盘口 ⇒ best 未知
        events = venue.submit(_taker_order("t3", Side.BUY))
        self.assertEqual(events[0].reason, SimulatedRejectReason.TAKER_NO_LIQUIDITY.value)

    def test_post_only_orders_remain_resting(self) -> None:
        venue = _venue(slippage=FixedBpsSlippage(10.0))
        venue.on_market_event(book_snapshot())
        maker = Order(
            client_order_id="m1", venue=Venue.BINANCE, symbol="BTCUSDT", side=Side.BUY,
            price=99.0, quantity=1.0, status=OrderStatus.PENDING_CREATE,
            created_at=ts(300_000), updated_at=ts(300_000),
            order_type="limit", post_only=True)
        accepted = venue.submit(maker)
        self.assertIsInstance(accepted[0], OrderAccepted)
        self.assertEqual(venue.order_view("m1").status, OrderStatus.OPEN)
        self.assertEqual(venue.poll(), ())


if __name__ == "__main__":
    unittest.main()
