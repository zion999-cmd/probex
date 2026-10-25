"""人类审查 P1（2026-10-24）：CollectorBatchSource 不得依赖 collector 私有字段。

P0017.5 实时 PAPER 只能通过 MultiSymbolMarketCollector 的**公开契约**取批次：
`symbols` / `set_batch_sink()` / `connected` / `connect()` / `pump_once()` / `stop()`。
本文件用契约级 fake collector 验证接线，并以源码静态守卫钉死不再出现
`_on_batch` / `_connected` 私有耦合。
"""

from __future__ import annotations

import pathlib
import unittest

from market.events.types import MarketEvent
from runtime.live_collector_source import CollectorBatchSource

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_FILE = REPO_ROOT / "runtime" / "live_collector_source.py"
COLLECTOR_FILE = REPO_ROOT / "connectors" / "binance" / "market_data" / "multi_collector.py"


class FakeCollector:
    """CollectorBatchSource 依赖的公开契约（duck typing；不触网）。"""

    def __init__(self, symbols=("BTCUSDT", "ETHUSDT")) -> None:
        self.symbols = tuple(symbols)
        self._sink = None
        self.sinks_installed: list[object] = []
        self.connected = False
        self.connect_calls = 0
        self.pump_calls = 0
        self.stop_calls = 0
        self._queued: dict[str, list[object]] = {s: [] for s in self.symbols}

    # ------------------------------------------------------- 公开契约
    def set_batch_sink(self, sink) -> object:
        previous = self._sink
        self._sink = sink
        self.sinks_installed.append(sink)
        return previous

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    def pump_once(self) -> dict[str, object]:
        self.pump_calls += 1
        for symbol, batches in self._queued.items():
            for batch in batches:
                if self._sink is not None:
                    self._sink(symbol, batch)
        self._queued = {s: [] for s in self.symbols}
        return {s: {} for s in self.symbols}

    def stop(self) -> None:
        self.stop_calls += 1

    # ------------------------------------------------------- test helpers
    def emit(self, symbol: str, batch: object) -> None:
        self._queued[symbol].append(batch)


class FakeBatch:
    def __init__(self, events: tuple[object, ...]) -> None:
        self.market_events = events


def _event(ts: int) -> MarketEvent:
    from tests.support import depth_snapshot_event
    return depth_snapshot_event(100 + ts, exchange_ts=ts, receive_ts=ts, process_ts=ts)


class CollectorBatchSourceContractTest(unittest.TestCase):
    def test_connects_when_not_connected_and_installs_sink_via_public_contract(self) -> None:
        collector = FakeCollector()
        source = CollectorBatchSource(collector)  # type: ignore[arg-type]
        self.assertEqual(collector.connect_calls, 1)
        # sink 经公开契约安装一次，并确实成为活动 sink
        self.assertEqual(len(collector.sinks_installed), 1)
        self.assertIs(collector._sink, collector.sinks_installed[0])
        self.assertIsNotNone(collector._sink)
        self.assertEqual(source.symbols, ("BTCUSDT", "ETHUSDT"))

    def test_does_not_reconnect_when_already_connected(self) -> None:
        collector = FakeCollector()
        collector.connect()
        source = CollectorBatchSource(collector)  # type: ignore[arg-type]
        self.assertEqual(collector.connect_calls, 1)
        _ = source

    def test_pump_returns_captured_events_per_symbol(self) -> None:
        collector = FakeCollector()
        source = CollectorBatchSource(collector)  # type: ignore[arg-type]
        e1, e2 = _event(1), _event(2)
        collector.emit("BTCUSDT", FakeBatch((e1, e2)))
        batches = source.pump()
        by_symbol = {b.symbol: b for b in batches}
        self.assertEqual(by_symbol["BTCUSDT"].events, (e1, e2))
        self.assertEqual(by_symbol["ETHUSDT"].events, ())
        self.assertIsNone(by_symbol["BTCUSDT"].source_error)
        # 每轮取空，不跨轮累积
        self.assertEqual([b for b in source.pump() if b.events], [])

    def test_pump_surfaces_collector_error_per_symbol(self) -> None:
        collector = FakeCollector()
        source = CollectorBatchSource(collector)  # type: ignore[arg-type]
        collector.pump_once = lambda: {"BTCUSDT": {"error": "BOOM"},  # type: ignore[method-assign]
                                         "ETHUSDT": {}}
        batches = source.pump()
        by_symbol = {b.symbol: b for b in batches}
        self.assertEqual(by_symbol["BTCUSDT"].source_error, "BOOM")
        self.assertEqual(by_symbol["BTCUSDT"].events, ())

    def test_existing_sink_still_called_and_receives_same_batch(self) -> None:
        collector = FakeCollector()
        seen: list[tuple[str, object]] = []
        collector.set_batch_sink(lambda symbol, batch: seen.append((symbol, batch)))
        source = CollectorBatchSource(collector)  # type: ignore[arg-type]
        batch = FakeBatch((_event(1),))
        collector.emit("BTCUSDT", batch)
        source.pump()
        self.assertEqual(seen, [("BTCUSDT", batch)])

    def test_close_stops_collector(self) -> None:
        collector = FakeCollector()
        source = CollectorBatchSource(collector)  # type: ignore[arg-type]
        source.close()
        self.assertEqual(collector.stop_calls, 1)

    def test_source_does_not_touch_collector_private_fields(self) -> None:
        source_text = SOURCE_FILE.read_text(encoding="utf-8")
        self.assertNotIn("_on_batch", source_text)
        self.assertNotIn("_connected", source_text)

    def test_collector_exposes_public_sink_and_connection_contract(self) -> None:
        collector_text = COLLECTOR_FILE.read_text(encoding="utf-8")
        self.assertIn("def set_batch_sink", collector_text)
        self.assertIn("def connected", collector_text)


if __name__ == "__main__":
    unittest.main()
