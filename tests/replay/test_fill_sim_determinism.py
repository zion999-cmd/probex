"""Replay：同一事件流多次回放必须得到完全相同的成交、订单状态与 PnL（SC-14 / SC-15）。

- 顺序完全由 Event Store 的 recorded ordinal 决定（`ReplaySource`），与 wall clock 无关；
- FULL 与 STEP 两种模式必须给出相同结果；
- telemetry（queue / fill_reason / event_ordinal）也参与比较，避免「结果一样但证据不同」。
"""

from __future__ import annotations

import unittest

from pathlib import Path

from execution.simulation.types import QueueState
from market.events.types import MarketEvent
from market.replay.source import ReplayMode, ReplaySource
from risk.limits import RiskLimits
from storage.events.reader import JsonlEventReader
from tests.sim_support import SimStack, book_delta, book_snapshot, sell_aggressor, ts
from tests.support import TempDirTestCase, write_store

#: 撤单脚本在此 ordinal 触发（订单在途时撤单）。
CANCEL_ORDINAL = 6
#: 挂单脚本：第一条 book 事件之后立刻挂单。
SUBMIT_AFTER_ORDINAL = 0


def scripted_events() -> tuple[MarketEvent, ...]:
    """包含 L2 减少、重复成交、trade-through、gap 与恢复的完整脚本。

    时间轴与 `submit_latency_ms = 20` 对齐：订单在 ts(20) 才进入盘口，
    因此第一条成交事件放在 ts(25)。
    """
    return (
        book_snapshot(update_id=1, bids=((100.0, 3.0),), asks=((101.0, 2.0),), offset=0),
        book_delta(2, 2, bids=((100.0, 3.5),), offset=1),
        sell_aggressor(1, price=100.0, quantity=1.2, offset=25),
        sell_aggressor(2, price=100.5, quantity=5.0, offset=26),  # 未触及我们的价位
        book_delta(3, 3, bids=((100.0, 2.0),), offset=27),  # L2 减少：不推进队列
        sell_aggressor(3, price=99.5, quantity=0.4, offset=28),  # trade-through
        sell_aggressor(2, price=100.0, quantity=1.0, offset=29),  # 重复 trade id
        book_delta(90, 90, bids=((100.0, 4.0),), offset=30),  # gap → STALE
        sell_aggressor(4, price=100.0, quantity=5.0, offset=31),  # 挂起期间：不成交
        book_snapshot(update_id=90, bids=((100.0, 4.0),), asks=((101.0, 2.0),), offset=32),
        sell_aggressor(5, price=100.0, quantity=4.0, offset=33),  # 清空重建后的队列
        sell_aggressor(6, price=100.0, quantity=0.6, offset=34),  # 成交 → FILLED
        book_delta(91, 91, bids=((100.0, 4.5),), offset=35),
        sell_aggressor(7, price=100.0, quantity=5.0, offset=36),  # 终态之后：不得复活
    )


def run_replay(
    path: Path,
    events: tuple[MarketEvent, ...],
    *,
    mode: ReplayMode = ReplayMode.FULL,
    cancel: bool = True,
) -> tuple:
    """把事件写入 Event Store 后按 recorded ordinal 顺序回放，返回完整状态快照。"""
    write_store(path, events)
    stack = SimStack.build(
        limits=RiskLimits(max_position_qty=10.0),
        submit_latency_ms=20,
        cancel_latency_ms=30,
        maker_fee_rate=0.0002,
    )
    source = ReplaySource(JsonlEventReader(path), mode=mode)
    iterator = source.iter_events() if mode is ReplayMode.FULL else _step_events(source)
    order_id: str | None = None
    for index, event in enumerate(iterator):
        stack.feed((event,))
        if index == SUBMIT_AFTER_ORDINAL and order_id is None:
            order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=event.process_ts)
            order_id = order.client_order_id
        elif cancel and index == CANCEL_ORDINAL and order_id is not None:
            current = stack.order(order_id)
            if not current.is_terminal:
                stack.cancel(current, now_ms=event.process_ts)
    return stack.state()


def _step_events(source: ReplaySource):
    while (event := source.next_event()) is not None:
        yield event


class FillSimDeterminismTest(TempDirTestCase):
    def test_sc15_two_full_replays_are_field_identical(self) -> None:
        events = scripted_events()

        first = run_replay(self.store_path("a.jsonl"), events)
        second = run_replay(self.store_path("b.jsonl"), events)

        self.assertEqual(first, second)
        self.assertTrue(first[5])  # 至少产生一笔模拟成交

    def test_sc15_full_and_step_modes_agree(self) -> None:
        events = scripted_events()

        self.assertEqual(
            run_replay(self.store_path("full.jsonl"), events, mode=ReplayMode.FULL),
            run_replay(self.store_path("step.jsonl"), events, mode=ReplayMode.STEP),
        )

    def test_sc14_replay_from_the_store_preserves_recorded_order(self) -> None:
        path = self.store_path("fill-sim.jsonl")
        records = write_store(path, scripted_events())
        stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0), submit_latency_ms=20, cancel_latency_ms=30)
        order_id: str | None = None

        for index, record in enumerate(JsonlEventReader(path)):
            stack.feed((record.event,))
            if index == SUBMIT_AFTER_ORDINAL and order_id is None:
                order_id = stack.submit_order(
                    stack.proposal(quantity=1.0, price=100.0), now_ms=record.event.process_ts
                ).client_order_id
            elif index == CANCEL_ORDINAL and order_id is not None:
                current = stack.order(order_id)
                if not current.is_terminal:
                    stack.cancel(current, now_ms=record.event.process_ts)

        self.assertEqual(stack.venue.event_ordinal, len(records))
        self.assertEqual(
            [record.ordinal for record in JsonlEventReader(path)], list(range(len(records)))
        )
        self.assertEqual([record.event_id for record in JsonlEventReader(path)],
                         [record.event_id for record in records])

    def test_duplicate_and_post_gap_trades_are_not_applied_twice(self) -> None:
        state = run_replay(self.store_path("once.jsonl"), scripted_events(), cancel=False)
        fills = state[5]
        view = state[4][0]

        self.assertEqual(len(fills), 2)
        self.assertEqual([fill.fill_reason.value for fill in fills], ["trade_through", "queue_consumed"])
        self.assertEqual([fill.event_ordinal for fill in fills], [5, 11])
        self.assertEqual(view.queue_rebuild_count, 1)
        self.assertIs(view.queue_state, QueueState.KNOWN)
        self.assertEqual(view.status.value, "FILLED")

    def test_cancel_race_is_deterministic(self) -> None:
        with_cancel = run_replay(self.store_path("cancel-a.jsonl"), scripted_events(), cancel=True)
        without_cancel = run_replay(self.store_path("cancel-b.jsonl"), scripted_events(), cancel=False)

        self.assertNotEqual(with_cancel, without_cancel)
        self.assertEqual(
            with_cancel, run_replay(self.store_path("cancel-c.jsonl"), scripted_events(), cancel=True)
        )
        self.assertEqual(with_cancel[4][0].cancel_requested_at, ts(29))
        self.assertEqual(with_cancel[4][0].cancel_effective_ts, ts(29) + 30)

    def test_venue_has_no_wall_clock_dependency(self) -> None:
        """证据：模拟器只消费事件时间（`event_ordinal` 与成交时间均来自事件）。"""
        fills = run_replay(self.store_path("clock.jsonl"), scripted_events(), cancel=False)[5]

        self.assertEqual([fill.exchange_ts for fill in fills], [ts(28), ts(34)])
        self.assertTrue(all(fill.event_ordinal >= 0 for fill in fills))


if __name__ == "__main__":
    unittest.main()
