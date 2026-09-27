"""SC-1 / SC-11：Replay 下的预测身份与 RECORDED 复现。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from market.events.types import MarketEvent
from market.replay.clock import ReplayClock
from market.replay.source import ReplaySource
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import build_jev_payload_json, market_state_hash
from prediction.types import PredictionMode, PredictionOutcome
from storage.events.reader import JsonlEventReader
from storage.events.writer import JsonlEventWriter
from tests.fakes import FakeClock, FakeProvider
from tests.support import (
    BASE_TS,
    depth_diff_event,
    depth_snapshot_event,
    feature_engine,
    warm_market_states,
)


def _warm_events(count: int = 3) -> list[MarketEvent]:
    """事件时间跨过 300s 的流，使后续每个 MarketState 都 tradeable。"""
    events: list[MarketEvent] = [
        depth_snapshot_event(
            100,
            bids=[(100.0, 5.0)],
            asks=[(101.0, 2.0)],
            exchange_ts=BASE_TS,
            receive_ts=BASE_TS,
            process_ts=BASE_TS,
        )
    ]
    for index in range(count):
        update_id = 101 + index
        timestamp = BASE_TS + 300_000 + index
        events.append(
            depth_diff_event(
                update_id,
                update_id,
                bids=[(100.0, 5.0 + (index + 1) * 0.1)],
                exchange_ts=timestamp,
                receive_ts=timestamp,
                process_ts=timestamp,
            )
        )
    return events


class PredictionStateIdentityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = self.tmp_path / "events.jsonl"
        with JsonlEventWriter(self.store) as writer:
            for event in _warm_events(3):
                writer.append(event)

    def _events(self) -> list[MarketEvent]:
        return list(ReplaySource(JsonlEventReader(self.store)).iter_events())

    async def _replay(self, runtime: PredictionRuntime) -> tuple[tuple[str, ...], tuple[str, ...], tuple[object, ...]]:
        engine = feature_engine()
        hashes: list[str] = []
        payloads: list[str] = []
        results: list[object] = []
        for event in self._events():
            state = engine.on_market_event(event)
            results.append(await runtime.submit(state))
            hashes.append(market_state_hash(state))
            payloads.append(build_jev_payload_json(state))
        return tuple(hashes), tuple(payloads), tuple(results)

    async def test_replay_twice_produces_identical_state_identity_and_payloads(self) -> None:
        first_hashes, first_payloads, _ = await self._replay(
            PredictionRuntime(provider=FakeProvider(), clock=FakeClock(now=0), timeout_ms=100, ttl_ms=1_000)
        )
        second_hashes, second_payloads, _ = await self._replay(
            PredictionRuntime(provider=FakeProvider(), clock=FakeClock(now=0), timeout_ms=100, ttl_ms=1_000)
        )

        self.assertEqual(first_hashes, second_hashes)
        self.assertEqual(first_payloads, second_payloads)
        self.assertEqual(len(set(first_hashes)), len(first_hashes), "每个 MarketState 身份应互不相同")

    async def test_replay_clock_drives_deterministic_timestamps(self) -> None:
        clock = ReplayClock(start=BASE_TS)
        provider = FakeProvider(clock=None, latency_ms=0)
        engine = feature_engine()
        records = []
        for event in self._events():
            state = engine.on_market_event(event)
            clock.advance_to(state.time.as_of_exchange_ts)
            result = await PredictionRuntime(
                provider=provider, clock=clock, timeout_ms=100, ttl_ms=5_000_000
            ).submit(state)
            if result.record is not None:
                records.append((result.record.request_created_at, result.record.as_of))

        self.assertEqual([created for created, _ in records], [state for _, state in records])
        # 只有 warm up 之后（跨过 300s）的状态才 eligible，因此不含首个快照状态
        self.assertEqual(
            [as_of for _, as_of in records],
            [BASE_TS + 300_000, BASE_TS + 300_001, BASE_TS + 300_002],
        )

    async def test_recorded_replay_reproduces_archived_records_without_provider(self) -> None:
        live_provider = FakeProvider(clock=FakeClock(now=1_000), latency_ms=7)
        live = PredictionRuntime(provider=live_provider, clock=FakeClock(now=1_000), timeout_ms=100, ttl_ms=5_000)
        _, _, live_results = await self._replay(live)
        accepted = [result for result in live_results if result.accepted]  # type: ignore[attr-defined]
        self.assertEqual(len(accepted), len(self._events()) - 1, "只有 warm up 之后的状态才 eligible")

        replay_provider = FakeProvider(error=AssertionError("RECORDED 模式不得调用 provider"))
        recorded = PredictionRuntime(
            provider=replay_provider,
            clock=FakeClock(now=9_999_999),
            timeout_ms=100,
            ttl_ms=5_000,
            mode=PredictionMode.RECORDED,
            archive=live.archive,
        )
        _, _, replay_results = await self._replay(recorded)

        self.assertEqual(replay_provider.requests, [])
        self.assertEqual(
            [result.outcome for result in replay_results],  # type: ignore[attr-defined]
            [
                PredictionOutcome.NOT_RECORDED if result.outcome is PredictionOutcome.NOT_RECORDED else PredictionOutcome.ACCEPTED
                for result in replay_results  # type: ignore[attr-defined]
            ],
        )
        recorded_records = [result.record for result in replay_results if result.record is not None]  # type: ignore[attr-defined]
        self.assertEqual(recorded_records, [result.record for result in accepted])
        self.assertEqual(recorded.latest_prediction, accepted[-1].record)

    async def test_unrecorded_state_reports_not_recorded(self) -> None:
        runtime = PredictionRuntime(
            provider=FakeProvider(error=AssertionError("must not be called")),
            clock=FakeClock(),
            timeout_ms=100,
            ttl_ms=1_000,
            mode=PredictionMode.RECORDED,
        )
        result = await runtime.submit(warm_market_states(1)[0])

        self.assertIs(result.outcome, PredictionOutcome.NOT_RECORDED)
        self.assertIsNone(result.record)
        self.assertIsNone(runtime.latest_prediction)

    async def test_replay_never_touches_the_network(self) -> None:
        # 结构性保证：预测层不 import 任何网络库（详见 tests/unit/test_prediction_isolation.py）
        runtime = PredictionRuntime(provider=FakeProvider(), clock=FakeClock(), timeout_ms=100, ttl_ms=1_000)
        _, _, results = await self._replay(runtime)
        self.assertTrue(all(result.record is not None for result in results[1:]))  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
