"""P0001.9.1 集成测试：live 运行时把真实报文形态接进 canonical pipeline。

覆盖 SC-1（depth → canonical events）、SC-2（snapshot 后 HEALTHY）、SC-3/SC-4（gap/resync）、
SC-7（mark 独立事实）、SC-11（时间戳）、SC-14（真实 TradePayload → fill simulation）。
"""

from __future__ import annotations

import json
import unittest

from execution.simulation import FeeSchedule, LatencyModel, SimulatedVenue
from market.book.market_book import MarketBook
from market.events.types import EventType, Venue
from market.health.state import BookHealth
from portfolio.types import Side
from storage.events.reader import JsonlEventReader
from market.replay.source import ReplaySource

from connectors.binance.market_data.endpoints import DEPTH_PATH, EXCHANGE_INFO_PATH, SERVER_TIME_PATH
from connectors.binance.market_data.runtime import LiveMarketDataRuntime, MarketDataBatch
from tests.live_support import (
    BINANCE_SYMBOL,
    FakeHttp,
    ScriptedTransport,
    agg_trade_message,
    depth_snapshot_payload,
    depth_update_message,
    exchange_info_payload,
    live_config,
    mark_price_message,
)
from tests.support import BASE_TS, TempDirTestCase, write_store
from tests.ws_stub_server import StubWebSocketServer


def build_runtime(**overrides: object) -> tuple[LiveMarketDataRuntime, ScriptedTransport, FakeHttp]:
    transport = ScriptedTransport()
    http = FakeHttp(
        responses={
            DEPTH_PATH: depth_snapshot_payload(last_update_id=100),
            EXCHANGE_INFO_PATH: exchange_info_payload(),
            SERVER_TIME_PATH: {"serverTime": BASE_TS + 42},
        }
    )
    runtime = LiveMarketDataRuntime(
        config=live_config(**overrides),  # type: ignore[arg-type]
        http_client=http,
        transport_factory=transport,
        clock=lambda: BASE_TS,
        sleeper=lambda _seconds: None,
    )
    runtime.connect()
    return runtime, transport, http


def _reached(health: str):
    """谓词：累积 batches 中是否出现过指定 book health。"""

    def predicate(batches: tuple[MarketDataBatch, ...]) -> bool:
        return any(
            state.quality.book_health.value == health for batch in batches for state in batch.states
        )

    return predicate


def pump_until(runtime: LiveMarketDataRuntime, predicate, *, max_pumps: int = 40) -> tuple[MarketDataBatch, ...]:
    """持续 pump 直到 `predicate(已累积的 batches)` 成立（两条连接轮转，顺序确定）。"""
    batches: list[MarketDataBatch] = []
    for _ in range(max_pumps):
        batches.append(runtime.pump_once(timeout_s=0.01))
        if predicate(tuple(batches)):
            return tuple(batches)
    raise AssertionError(f"condition not reached within {max_pumps} pumps: {[b.errors for b in batches]}")


def push_depth(transport: ScriptedTransport, text: str) -> None:
    transport.connection("public").push(text)


def push_market(transport: ScriptedTransport, text: str) -> None:
    transport.connection("market").push(text)


class LiveMarketDataTest(unittest.TestCase):
    def test_depth_speed_suffix_is_configurable(self) -> None:
        """P0001.9.1 §0.3：速度后缀必须可配置（100ms/500ms 有数据；250ms 后缀无效）。"""
        _, transport, _ = build_runtime(depth_speed="500ms")

        self.assertIn("btcusdt@depth@500ms", transport.connection("public").sent[0])

    def test_sc1_sc2_depth_stream_and_snapshot_establish_health(self) -> None:
        runtime, transport, http = build_runtime()
        push_depth(transport, depth_update_message(first_update_id=99, last_update_id=100))

        batches = pump_until(
            runtime,
            lambda batches: any(state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states),
        )

        events = [event for batch in batches for event in batch.market_events]
        self.assertEqual([event.event_type for event in events], [EventType.BOOK_DELTA, EventType.BOOK_SNAPSHOT])
        self.assertTrue(all(event.receive_ts == BASE_TS for event in events))
        self.assertEqual(http.calls[0][0], DEPTH_PATH)
        telemetry = runtime.telemetry
        self.assertEqual((telemetry.depth_event_count, telemetry.resync_count, telemetry.snapshot_count), (1, 1, 1))
        self.assertIsNotNone(telemetry.snapshot_latency_ms)

    def test_futures_aggregated_deltas_do_not_trigger_false_gaps(self) -> None:
        """P0001.9.1.1 SC-2：真实 Futures 形态（U 跳跃、pu 接续）必须保持 HEALTHY。"""
        runtime, transport, http = build_runtime()
        http.responses[DEPTH_PATH] = depth_snapshot_payload(last_update_id=1000)

        # 锚点增量：满足文档窗口规则（U <= snapshot.last+1 <= u）
        push_depth(transport, depth_update_message(first_update_id=1001, last_update_id=12000, previous_update_id=1000))
        batches = pump_until(runtime, _reached("healthy"))
        push_depth(transport, depth_update_message(first_update_id=12050, last_update_id=30000, previous_update_id=12000))
        push_depth(transport, depth_update_message(first_update_id=30040, last_update_id=50000, previous_update_id=30000))
        batches += pump_until(runtime, lambda seen: runtime.telemetry.depth_event_count >= 3)

        healths = [state.quality.book_health for batch in batches for state in batch.states]
        first_healthy = healths.index(BookHealth.HEALTHY)

        self.assertEqual(runtime.telemetry.depth_gap_count, 0)
        self.assertEqual(runtime.telemetry.resync_count, 1)  # 仅初始快照
        self.assertEqual(runtime.telemetry.depth_event_count, 3)
        # 快照之后不再出现任何非 HEALTHY 状态（没有假 gap 引起的 STALE churn）
        self.assertTrue(all(health is BookHealth.HEALTHY for health in healths[first_healthy:]))

    def test_futures_real_loss_triggers_gap_and_resync(self) -> None:
        """P0001.9.1.1 SC-3：`pu != last` 才是真实丢失 → STALE → resync。"""
        runtime, transport, http = build_runtime()
        http.responses[DEPTH_PATH] = depth_snapshot_payload(last_update_id=1000)
        push_depth(transport, depth_update_message(first_update_id=1001, last_update_id=12000, previous_update_id=1000))
        pump_until(runtime, _reached("healthy"))

        # 重同步时拿到的是「丢号之后」的新快照
        http.responses[DEPTH_PATH] = depth_snapshot_payload(last_update_id=30000)
        push_depth(transport, depth_update_message(first_update_id=12050, last_update_id=30000, previous_update_id=99000))
        stale = pump_until(runtime, _reached("stale"))
        push_depth(transport, depth_update_message(first_update_id=30040, last_update_id=40000, previous_update_id=30000))
        recovered = pump_until(runtime, _reached("healthy"))

        self.assertEqual(runtime.telemetry.depth_gap_count, 1)
        self.assertEqual(runtime.telemetry.snapshot_count, 2)
        self.assertTrue(stale)
        self.assertTrue(recovered)

    def test_sc11_source_timestamps_are_preserved(self) -> None:
        runtime, transport, _ = build_runtime()
        push_depth(
            transport,
            depth_update_message(first_update_id=99, last_update_id=100, exchange_ts=BASE_TS - 250),
        )

        batches = pump_until(runtime, lambda batches: any(batch.market_events for batch in batches))
        event = next(event for batch in batches for event in batch.market_events)

        self.assertEqual(event.exchange_ts, BASE_TS - 250)
        self.assertEqual(event.receive_ts, BASE_TS)
        self.assertEqual(runtime.telemetry.event_lag_ms, 250)

    def test_sc3_sc4_gap_is_detected_and_resynced(self) -> None:
        runtime, transport, http = build_runtime()
        push_depth(transport, depth_update_message(first_update_id=99, last_update_id=100))
        pump_until(
            runtime,
            lambda batches: any(state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states),
        )
        http.responses[DEPTH_PATH] = depth_snapshot_payload(last_update_id=200, bids=((100.0, 4.0),))

        push_depth(transport, depth_update_message(first_update_id=150, last_update_id=150))  # gap
        gap_batches = pump_until(
            runtime,
            lambda batches: any(state.quality.book_health is BookHealth.STALE for batch in batches for state in batch.states),
        )
        self.assertEqual(runtime.telemetry.depth_gap_count, 1)

        push_depth(transport, depth_update_message(first_update_id=200, last_update_id=200))
        recovery = pump_until(
            runtime,
            lambda batches: any(state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states),
        )

        self.assertTrue(gap_batches)
        self.assertEqual(runtime.telemetry.snapshot_count, 2)
        self.assertEqual(recovery[-1].states[-1].quality.book_health, BookHealth.HEALTHY)

    def test_sc7_mark_price_is_an_independent_observation(self) -> None:
        runtime, transport, _ = build_runtime()
        push_market(transport, mark_price_message(price=101.25))

        batches = pump_until(runtime, lambda batches: any(batch.mark is not None for batch in batches))
        mark = next(batch.mark for batch in batches if batch.mark is not None)

        self.assertIsNotNone(mark)
        self.assertEqual(mark.price, 101.25)
        self.assertEqual(runtime.latest_mark.price, 101.25)
        self.assertEqual(runtime.telemetry.mark_update_count, 1)
        self.assertEqual(runtime.telemetry.mark_age_ms, 0)
        self.assertEqual([event for batch in batches for event in batch.market_events], [])

    def test_sc6_agg_trade_events_and_duplicate_filtering(self) -> None:
        runtime, transport, _ = build_runtime()
        push_market(transport, agg_trade_message(aggregate_trade_id=5, is_buyer_maker=True))
        push_market(transport, agg_trade_message(aggregate_trade_id=5, is_buyer_maker=True))
        push_market(transport, agg_trade_message(aggregate_trade_id=6, is_buyer_maker=False))
        batches = pump_until(
            runtime,
            lambda batches: len(
                [e for batch in batches for e in batch.market_events if e.event_type is EventType.TRADE]
            )
            >= 2,
        )

        trades = [event for batch in batches for event in batch.market_events]
        event = trades[0]
        self.assertIs(event.event_type, EventType.TRADE)
        self.assertEqual(event.payload.aggressor.value, "sell")  # m=true → sell aggressor
        self.assertEqual([event.payload.aggregate_trade_id for event in trades], [5, 6])
        self.assertEqual((runtime.telemetry.agg_trade_count, runtime.telemetry.duplicate_trade_count), (2, 1))

    def test_ack_messages_are_counted_but_not_dispatched(self) -> None:
        runtime, transport, _ = build_runtime()
        push_depth(transport, json.dumps({"result": None, "id": 1}))

        batch = runtime.pump_once(timeout_s=0.01)

        self.assertTrue(batch.empty)
        self.assertFalse(batch.timed_out)
        self.assertEqual(runtime.telemetry.ack_count, 1)

    def test_sc8_exchange_info_and_server_time(self) -> None:
        runtime, _, _ = build_runtime()

        rules = runtime.load_trading_rules()
        offset = runtime.measure_server_time_offset()

        self.assertAlmostEqual(rules.tick_size, 0.1)
        self.assertTrue(rules.is_trading)
        self.assertEqual(offset, 42)
        self.assertEqual(runtime.trading_rules, rules)

    def test_subscription_is_sent_on_connect(self) -> None:
        _, transport, _ = build_runtime()

        public = transport.connection("public").sent[0]
        market = transport.connection("market").sent[0]

        self.assertIn("btcusdt@depth", public)  # 默认速度（无 @speed 后缀；250ms 后缀实测无数据）
        self.assertIn("btcusdt@aggTrade", market)
        self.assertIn("btcusdt@markPrice@1s", market)
        self.assertIn("/public/stream?streams=", transport.urls[0])
        self.assertIn("/market/stream?streams=", transport.urls[1])

    def test_run_summary_reports_progress(self) -> None:
        runtime, transport, _ = build_runtime()
        for index in range(5):
            push_depth(transport, depth_update_message(first_update_id=99, last_update_id=100))

        summary = runtime.run(max_messages=3, timeout_s=5.0)

        self.assertGreaterEqual(summary.messages_processed, 3)
        self.assertIsNotNone(summary.last_state)
        self.assertGreaterEqual(summary.telemetry.depth_event_count, 2)


class LiveToFillSimulationTest(TempDirTestCase):
    def test_sc14_recorded_live_trades_drive_the_fill_simulator(self) -> None:
        """真实归一化出来的 TradePayload → Event Store → Replay → SimulatedVenue 成交。"""
        runtime, transport, _ = build_runtime()
        push_depth(transport, depth_update_message(first_update_id=99, last_update_id=100))
        push_market(transport, agg_trade_message(aggregate_trade_id=5, price=100.0, quantity=2.0, is_buyer_maker=True))
        push_market(transport, agg_trade_message(aggregate_trade_id=6, price=100.0, quantity=2.0, is_buyer_maker=True))

        def enough(batches) -> bool:
            trades = [e for batch in batches for e in batch.market_events if e.event_type is EventType.TRADE]
            healthy = any(
                state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states
            )
            return len(trades) == 2 and healthy

        batches = pump_until(runtime, enough)
        recorded = tuple(event for batch in batches for event in batch.market_events)
        self.assertEqual(len([e for e in recorded if e.event_type is EventType.TRADE]), 2)
        path = self.store_path("live.jsonl")
        write_store(path, recorded)

        venue = SimulatedVenue(
            symbol=BINANCE_SYMBOL,
            latency=LatencyModel(submit_latency_ms=0, cancel_latency_ms=0),
            fee_schedule=FeeSchedule(maker_fee_rate=0.0002, fee_asset="USDT"),
            book=MarketBook(Venue.BINANCE, BINANCE_SYMBOL),
        )
        opened = venue.submit(_market_making_order())
        self.assertEqual(len(opened), 1)

        for event in ReplaySource(JsonlEventReader(path)).iter_events():
            venue.on_market_event(event)
        events = venue.poll()

        self.assertEqual(len(venue.fills), 1)
        self.assertEqual(venue.fills[0].fill_reason.value, "queue_consumed")
        # 初始队列 = 快照里 100.0 档位的可见量 3.0；两笔各 2.0 的卖单先清队列再成交 1.0
        self.assertEqual(venue.fills[0].quantity, 1.0)
        self.assertEqual(venue.order_view(_ORDER_ID).initial_queue_ahead, 3.0)
        self.assertEqual([type(event).__name__ for event in events], ["FillReceived"])
        self.assertIs(venue.order_view(_ORDER_ID).queue_state.value, "known")


_ORDER_ID = "sim-live-0001"


def _market_making_order():
    from execution.types import Order, OrderStatus

    return Order(
        client_order_id=_ORDER_ID,
        venue=Venue.BINANCE,
        symbol=BINANCE_SYMBOL,
        side=Side.BUY,
        price=100.0,
        quantity=2.0,
        status=OrderStatus.OPEN,
        created_at=BASE_TS,
        updated_at=BASE_TS,
        post_only=True,
    )


class LiveMarketDataOverRealSocketTest(unittest.TestCase):
    """真实 socket：stub WS 服务端 + 真实传输层 + 运行时。"""

    def setUp(self) -> None:
        self.server = StubWebSocketServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.http = FakeHttp(
            responses={
                DEPTH_PATH: depth_snapshot_payload(last_update_id=100),
                EXCHANGE_INFO_PATH: exchange_info_payload(),
                SERVER_TIME_PATH: {"serverTime": BASE_TS},
            }
        )
        self.runtime = LiveMarketDataRuntime(
            config=live_config(ws_host=self.server.ws_host, connect_timeout_s=5.0, read_timeout_s=5.0),
            http_client=self.http,
            clock=lambda: BASE_TS,
            sleeper=lambda _seconds: None,
        )
        self.addCleanup(self.runtime.close)

    def test_depth_message_over_a_real_socket_becomes_healthy(self) -> None:
        self.runtime.connect()
        self.server.wait_until_connected(2)
        self.server.send("/public", depth_update_message(first_update_id=99, last_update_id=100))

        batches: list[MarketDataBatch] = []
        for _ in range(6):
            batches.append(self.runtime.pump_once(timeout_s=5.0))
            if any(state.quality.book_health is BookHealth.HEALTHY for state in batches[-1].states):
                break

        self.assertTrue(any(state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states))
        self.assertEqual(self.runtime.telemetry.snapshot_count, 1)
        self.assertEqual(self.runtime.telemetry.ws_connect_count, 2)


if __name__ == "__main__":
    unittest.main()
