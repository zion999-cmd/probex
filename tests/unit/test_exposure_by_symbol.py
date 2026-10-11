"""P0017.2 Batch 1-3：OrderTracker 按 symbol 在途聚合（守恒）。"""

from __future__ import annotations

import unittest

from portfolio.types import Side
from market.events.types import Venue
from execution.tracker import OrderTracker
from execution.types import Order, OrderStatus
from risk.types import OrderCorrelation


def _order(client_id: str, *, symbol: str, side: Side, quantity: float, price: float,
           filled: float = 0.0, status: OrderStatus = OrderStatus.OPEN,
           avg_fill_price: float | None = None) -> Order:
    return Order(
        client_order_id=client_id, venue=Venue.BINANCE, symbol=symbol, side=side, price=price,
        quantity=quantity, status=status, created_at=1, updated_at=1,
        filled_quantity=filled,
        avg_fill_price=price if filled > 0 and avg_fill_price is None else (avg_fill_price or 0.0),
        correlation=OrderCorrelation(decision_id=f"d-{client_id}", instrument_id=f"{symbol}:binance",
                                      venue_id="binance", prediction_id="p"))


class ExposureBySymbolTest(unittest.TestCase):
    def test_confirmed_exposure_is_remaining_notional(self) -> None:
        tracker = OrderTracker(session_id="s")
        # 数量 2、价格 100、成交 0.5 ⇒ remaining 1.5 ⇒ 150
        tracker.register(_order("o1", symbol="BTCUSDT", side=Side.BUY, quantity=2.0,
                                  price=100.0, filled=0.5, status=OrderStatus.PARTIALLY_FILLED))
        by = tracker.exposure_by_symbol()
        self.assertAlmostEqual(by["BTCUSDT"]["confirmed"], 150.0)

    def test_conservation_sum_equals_total_pending(self) -> None:
        tracker = OrderTracker(session_id="s")
        tracker.register(_order("b1", symbol="BTCUSDT", side=Side.BUY, quantity=1.0, price=100.0))
        tracker.register(_order("e1", symbol="ETHUSDT", side=Side.SELL, quantity=2.0, price=50.0))
        by = tracker.exposure_by_symbol()
        total = sum(v["total"] for v in by.values())
        self.assertAlmostEqual(total, tracker.total_pending_exposure())
        self.assertAlmostEqual(by["BTCUSDT"]["confirmed"], 100.0)
        self.assertAlmostEqual(by["ETHUSDT"]["confirmed"], 100.0)

    def test_lost_counts_as_uncertain(self) -> None:
        tracker = OrderTracker(session_id="s")
        tracker.register(_order("l1", symbol="BTCUSDT", side=Side.BUY, quantity=1.0,
                                  price=100.0, status=OrderStatus.LOST))
        by = tracker.exposure_by_symbol()
        self.assertAlmostEqual(by["BTCUSDT"]["uncertain"], 100.0)
        self.assertAlmostEqual(by["BTCUSDT"]["confirmed"], 0.0)

    def test_terminal_orders_contribute_nothing(self) -> None:
        tracker = OrderTracker(session_id="s")
        tracker.register(_order("f1", symbol="BTCUSDT", side=Side.BUY, quantity=1.0,
                                  price=100.0, filled=1.0, status=OrderStatus.FILLED))
        tracker.register(_order("c1", symbol="BTCUSDT", side=Side.BUY, quantity=1.0,
                                  price=100.0, status=OrderStatus.CANCELED))
        self.assertEqual(tracker.exposure_by_symbol(), {})


if __name__ == "__main__":
    unittest.main()
