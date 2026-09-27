"""SC-2 / SC-9 / SC-10：PredictionRuntime 端到端。"""

from __future__ import annotations

import asyncio
import unittest

from prediction.errors import PredictionTransportError
from prediction.providers.base import PredictionProvider
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import build_jev_payload_json, market_state_hash
from prediction.types import (
    PredictionArchive,
    PredictionMode,
    PredictionOutcome,
    ProviderStatus,
)
from tests.fakes import FakeClock, FakeProvider, response_json
from tests.support import depth_diff_event, feature_engine, warm_market_states


class PredictionRuntimeAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    def _runtime(
        self,
        provider: PredictionProvider | None = None,
        *,
        clock: FakeClock | None = None,
        max_inflight: int = 1,
        base_backoff_ms: int = 1_000,
        max_backoff_ms: int = 8_000,
        archive: PredictionArchive | None = None,
        mode: PredictionMode = PredictionMode.LIVE_REQUERY,
    ) -> tuple[PredictionRuntime, FakeClock, PredictionProvider]:
        resolved_clock = clock if clock is not None else FakeClock(now=1_700_000_000_000)
        resolved_provider: PredictionProvider = (
            provider if provider is not None else FakeProvider(clock=resolved_clock, latency_ms=25)
        )
        runtime = PredictionRuntime(
            provider=resolved_provider,
            clock=resolved_clock,
            timeout_ms=5_000,
            ttl_ms=2_000,
            max_inflight=max_inflight,
            base_backoff_ms=base_backoff_ms,
            max_backoff_ms=max_backoff_ms,
            archive=archive,
            mode=mode,
        )
        return runtime, resolved_clock, resolved_provider

    async def test_sc2_market_engine_is_not_blocked(self) -> None:
        engine = feature_engine()
        state = warm_market_states(1, engine=engine)[0]
        gate = asyncio.Event()
        runtime, _, _ = self._runtime(FakeProvider(gate=gate))

        task = asyncio.create_task(runtime.submit(state))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(runtime.scheduler.inflight, 1, "请求应该仍在飞行中")

        # 请求未返回期间，市场状态继续推进
        timestamp = state.time.as_of_exchange_ts + 1_000
        later = engine.on_market_event(
            depth_diff_event(
                102,
                102,
                bids=[(100.0, 9.0)],
                exchange_ts=timestamp,
                receive_ts=timestamp,
                process_ts=timestamp,
            )
        )
        self.assertEqual(later.time.event_ordinal, state.time.event_ordinal + 1)
        self.assertEqual(later.price.bid_size, 9.0)
        self.assertNotEqual(market_state_hash(later), market_state_hash(state))

        gate.set()
        result = await task
        self.assertTrue(result.accepted)

    async def test_sc9_record_is_traceable(self) -> None:
        state = warm_market_states(1)[0]
        runtime, clock, provider = self._runtime()
        result = await runtime.submit(state)

        self.assertIs(result.outcome, PredictionOutcome.ACCEPTED)
        record = result.record
        assert record is not None
        assert result.request is not None

        self.assertEqual(record.market_state_hash, market_state_hash(state))
        self.assertEqual(record.market_state_hash, result.request.market_state_hash)
        self.assertEqual(record.feature_schema_version, state.feature_schema_version)
        self.assertEqual(record.question_schema_version, "jev-market-v1")
        self.assertEqual(record.provider, provider.provider)
        self.assertEqual(record.model, provider.model)
        self.assertEqual(record.raw_response, response_json())
        self.assertEqual(record.as_of, state.time.as_of_exchange_ts)
        self.assertEqual(record.latency_ms, 25)
        self.assertEqual(record.response_received_at, clock.now())

    async def test_payload_actually_sent_is_the_canonical_payload(self) -> None:
        state = warm_market_states(1)[0]
        runtime, _, provider = self._runtime()
        await runtime.submit(state)

        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(provider.requests[0].payload_json, build_jev_payload_json(state))

    async def test_accepted_record_enters_archive(self) -> None:
        state = warm_market_states(1)[0]
        runtime, _, _ = self._runtime()
        result = await runtime.submit(state)

        assert result.record is not None
        self.assertEqual(len(runtime.archive), 1)
        self.assertEqual(runtime.latest_prediction, result.record)
        self.assertEqual(runtime.archive.find(market_state_hash=market_state_hash(state), question_schema_version="jev-market-v1"), result.record)

    async def test_repeated_state_produces_two_records_for_the_same_state(self) -> None:
        state = warm_market_states(1)[0]
        runtime, _, provider = self._runtime()

        first = await runtime.submit(state)
        second = await runtime.submit(state)

        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertEqual(len(provider.requests), 2)  # type: ignore[attr-defined]
        self.assertIs(runtime.latest_prediction, second.record)
        # archive 保留两条证据，但 find 返回首次那条
        self.assertEqual(len(runtime.archive.records), 2)  # type: ignore[attr-defined]
        self.assertEqual(
            runtime.archive.find(market_state_hash=market_state_hash(state), question_schema_version="jev-market-v1"),
            first.record,
        )

    async def test_not_eligible_state_is_skipped_without_calling_provider(self) -> None:
        engine = feature_engine()
        cold = engine.on_market_event(
            depth_diff_event(101, 101, bids=[(100.0, 5.0)], exchange_ts=1_700_000_000_000)
        )
        runtime, _, provider = self._runtime()

        result = await runtime.submit(cold)

        self.assertIs(result.outcome, PredictionOutcome.NOT_ELIGIBLE)
        self.assertIsNone(result.request)
        self.assertIsNone(result.record)
        self.assertEqual(provider.requests, [])
        self.assertEqual(len(runtime.archive), 0)

    async def test_backoff_skips_then_recovers(self) -> None:
        clock = FakeClock(now=10_000)
        provider = FakeProvider(error=PredictionTransportError("down"), clock=clock)
        runtime, _, _ = self._runtime(provider, clock=clock)
        state = warm_market_states(1)[0]

        failed = await runtime.submit(state)
        self.assertIs(failed.outcome, PredictionOutcome.TRANSPORT_ERROR)
        self.assertIs(runtime.provider_status, ProviderStatus.BACKING_OFF)
        self.assertEqual(runtime.next_retry_at, 11_000)

        skipped = await runtime.submit(state)
        self.assertIs(skipped.outcome, PredictionOutcome.SKIPPED_BACKOFF)
        self.assertEqual(len(provider.requests), 1, "backoff 期间不得再打 provider")

        clock.advance(1_000)
        self.assertIs(runtime.provider_status, ProviderStatus.DEGRADED)

        provider.error = None
        recovered = await runtime.submit(state)

        self.assertIs(recovered.outcome, PredictionOutcome.ACCEPTED)
        self.assertIs(runtime.provider_status, ProviderStatus.HEALTHY)
        self.assertEqual(runtime.next_retry_at, None)
        self.assertEqual(runtime.consecutive_failures, 0)

    async def test_backoff_grows_exponentially(self) -> None:
        clock = FakeClock(now=0)
        provider = FakeProvider(error=PredictionTransportError("down"), clock=clock)
        runtime, _, _ = self._runtime(provider, clock=clock, base_backoff_ms=100, max_backoff_ms=250)
        state = warm_market_states(1)[0]

        await runtime.submit(state)
        self.assertEqual(runtime.next_retry_at, 100)
        clock.advance(100)
        await runtime.submit(state)
        self.assertEqual(runtime.next_retry_at, 300)  # 100 + 200
        clock.advance(200)
        await runtime.submit(state)
        self.assertEqual(runtime.next_retry_at, 550)  # 300 + min(400, 250)

    async def test_submissions_log_covers_every_outcome(self) -> None:
        clock = FakeClock(now=0)
        runtime, _, _ = self._runtime(
            FakeProvider(error=PredictionTransportError("down"), clock=clock), clock=clock
        )
        state = warm_market_states(1)[0]

        await runtime.submit(state)
        await runtime.submit(state)

        self.assertEqual(
            [result.outcome for result in runtime.submissions],
            [PredictionOutcome.TRANSPORT_ERROR, PredictionOutcome.SKIPPED_BACKOFF],
        )

    async def test_live_requery_calls_provider_again(self) -> None:
        state = warm_market_states(1)[0]
        runtime = PredictionRuntime(
            provider=FakeProvider(clock=FakeClock()),
            clock=FakeClock(),
            timeout_ms=100,
            ttl_ms=1_000,
        )
        await runtime.submit(state)
        await runtime.submit(state)

        self.assertEqual(len(runtime.submissions), 2)  # type: ignore[attr-defined]
        self.assertEqual(len(runtime.archive.records), 2)  # type: ignore[attr-defined]

    async def test_mode_is_part_of_the_record(self) -> None:
        state = warm_market_states(1)[0]
        live = PredictionRuntime(provider=FakeProvider(), clock=FakeClock(), timeout_ms=100, ttl_ms=1_000)
        live_result = await live.submit(state)
        assert live_result.record is not None
        self.assertIs(live_result.record.mode, PredictionMode.LIVE_REQUERY)

        recorded = PredictionRuntime(
            provider=FakeProvider(),
            clock=FakeClock(),
            timeout_ms=100,
            ttl_ms=1_000,
            mode=PredictionMode.RECORDED,
            archive=live.archive,
        )
        recorded_result = await recorded.submit(state)
        assert recorded_result.record is not None
        self.assertIs(recorded_result.record.mode, PredictionMode.LIVE_REQUERY)


if __name__ == "__main__":
    unittest.main()
