"""净持仓：加仓 / 部分平仓 / 全部平仓 / 反手（SC-3）+ 未知 ≠ 0（SC-6）。"""

from __future__ import annotations

import unittest

from portfolio.position import Position, apply_fill
from portfolio.types import InvalidFillError, Side
from tests.support import make_fill


def _flat(symbol: str = "BTCUSDT") -> Position:
    return Position(symbol=symbol)


class PositionInvariantTest(unittest.TestCase):
    def test_direction_semantics(self) -> None:
        self.assertTrue(_flat().is_flat)
        self.assertEqual(_flat().direction, "flat")
        self.assertEqual(Position(symbol="BTCUSDT", qty=1.0, avg_entry_price=100.0).direction, "long")
        self.assertEqual(Position(symbol="BTCUSDT", qty=-1.0, avg_entry_price=100.0).direction, "short")

    def test_flat_position_must_have_zero_avg_price(self) -> None:
        with self.assertRaises(InvalidFillError):
            Position(symbol="BTCUSDT", qty=0.0, avg_entry_price=100.0)

    def test_open_position_must_have_positive_avg_price(self) -> None:
        with self.assertRaises(InvalidFillError):
            Position(symbol="BTCUSDT", qty=1.0, avg_entry_price=0.0)

    def test_invalid_mark_price_rejected(self) -> None:
        with self.assertRaises(InvalidFillError):
            Position(symbol="BTCUSDT", qty=1.0, avg_entry_price=100.0, mark_price=0.0)


class PositionMathTest(unittest.TestCase):
    def test_open_from_flat(self) -> None:
        update = apply_fill(_flat(), make_fill("f1", Side.BUY, 100.0, 2.0))

        self.assertEqual(update.position.qty, 2.0)
        self.assertEqual(update.position.avg_entry_price, 100.0)
        self.assertEqual(update.realized_delta, 0.0)
        self.assertEqual(update.opened_qty, 2.0)
        self.assertFalse(update.is_reversal)

    def test_adding_to_position_uses_weighted_average(self) -> None:
        position = Position(symbol="BTCUSDT", qty=2.0, avg_entry_price=100.0)

        update = apply_fill(position, make_fill("f2", Side.BUY, 110.0, 2.0))

        self.assertEqual(update.position.qty, 4.0)
        self.assertEqual(update.position.avg_entry_price, 105.0)  # (2*100 + 2*110) / 4
        self.assertEqual(update.realized_delta, 0.0)

    def test_partial_close_keeps_average_and_realizes_pnl(self) -> None:
        # 提案示例：long 2 @100，sell 1 @110 → qty 1、avg 100、realized +10
        position = Position(symbol="BTCUSDT", qty=2.0, avg_entry_price=100.0)

        update = apply_fill(position, make_fill("f3", Side.SELL, 110.0, 1.0))

        self.assertEqual(update.position.qty, 1.0)
        self.assertEqual(update.position.avg_entry_price, 100.0)
        self.assertEqual(update.realized_delta, 10.0)
        self.assertEqual(update.position.realized_pnl, 10.0)
        self.assertEqual(update.closed_qty, 1.0)
        self.assertEqual(update.opened_qty, 0.0)

    def test_short_partial_close(self) -> None:
        position = Position(symbol="BTCUSDT", qty=-2.0, avg_entry_price=100.0)

        update = apply_fill(position, make_fill("f4", Side.BUY, 90.0, 1.0))

        self.assertEqual(update.position.qty, -1.0)
        self.assertEqual(update.realized_delta, 10.0)  # short 在下跌中获利
        self.assertEqual(update.position.avg_entry_price, 100.0)

    def test_full_close_resets_average_and_realizes_pnl(self) -> None:
        position = Position(symbol="BTCUSDT", qty=2.0, avg_entry_price=100.0)

        update = apply_fill(position, make_fill("f5", Side.SELL, 120.0, 2.0))

        self.assertEqual(update.position.qty, 0.0)
        self.assertEqual(update.position.avg_entry_price, 0.0)
        self.assertEqual(update.realized_delta, 40.0)
        self.assertTrue(update.position.is_flat)

    def test_reversal_splits_into_close_and_open_legs(self) -> None:
        # 提案示例：long 1 @100，sell 2 @110 → 平掉 long 1（+10），再开 short 1 @110
        position = Position(symbol="BTCUSDT", qty=1.0, avg_entry_price=100.0)

        update = apply_fill(position, make_fill("f6", Side.SELL, 110.0, 2.0))

        self.assertEqual(update.position.qty, -1.0)
        self.assertEqual(update.position.avg_entry_price, 110.0)  # 不混合平均价
        self.assertEqual(update.realized_delta, 10.0)
        self.assertEqual(update.closed_qty, 1.0)
        self.assertEqual(update.opened_qty, 1.0)
        self.assertTrue(update.is_reversal)

    def test_reversal_from_short_to_long(self) -> None:
        position = Position(symbol="BTCUSDT", qty=-1.0, avg_entry_price=100.0)

        update = apply_fill(position, make_fill("f7", Side.BUY, 95.0, 3.0))

        self.assertEqual(update.position.qty, 2.0)
        self.assertEqual(update.position.avg_entry_price, 95.0)
        self.assertEqual(update.realized_delta, 5.0)  # short 在 95 平仓获利
        self.assertEqual(update.closed_qty, 1.0)
        self.assertEqual(update.opened_qty, 2.0)

    def test_realized_pnl_accumulates_across_updates(self) -> None:
        position = Position(symbol="BTCUSDT", qty=2.0, avg_entry_price=100.0)
        first = apply_fill(position, make_fill("f8", Side.SELL, 110.0, 1.0))
        second = apply_fill(first.position, make_fill("f9", Side.SELL, 120.0, 1.0))

        self.assertEqual(second.position.qty, 0.0)
        self.assertEqual(second.position.realized_pnl, 30.0)

    def test_symbol_mismatch_is_rejected(self) -> None:
        with self.assertRaises(InvalidFillError):
            apply_fill(Position(symbol="BTCUSDT"), make_fill("f10", Side.BUY, 100.0, 1.0, symbol="ETHUSDT"))


class PositionMarkTest(unittest.TestCase):
    def test_unrealized_requires_mark(self) -> None:
        position = Position(symbol="BTCUSDT", qty=1.0, avg_entry_price=100.0)
        self.assertIsNone(position.unrealized_pnl)  # 未知 ≠ 0
        self.assertIsNone(position.notional)

        marked = position.with_mark(110.0)
        self.assertEqual(marked.unrealized_pnl, 10.0)
        self.assertEqual(marked.notional, 110.0)

    def test_flat_position_unrealized_is_zero(self) -> None:
        flat = Position(symbol="BTCUSDT")
        self.assertEqual(flat.unrealized_pnl, 0.0)
        self.assertEqual(flat.notional, 0.0)

    def test_mark_does_not_change_realized(self) -> None:
        position = Position(symbol="BTCUSDT", qty=1.0, avg_entry_price=100.0, realized_pnl=7.0)

        marked = position.with_mark(90.0)

        self.assertEqual(marked.realized_pnl, 7.0)
        self.assertEqual(marked.unrealized_pnl, -10.0)


if __name__ == "__main__":
    unittest.main()
