"""SC-4：超时不得产生伪 Prediction，且不阻塞后续请求。"""

from __future__ import annotations

import asyncio
import unittest

from prediction.errors import PredictionTimeoutError
from prediction.runtime import PredictionRuntime
from prediction.types import PredictionOutcome, ProviderStatus
from tests.fakes import FakeClock, FakeProvider, response_json
from tests.support import warm_market_states


def _runtime(*, gate: asyncio.Event, clock: FakeClock, **overrides: object) -> PredictionRuntime:
    provider = FakeProvider(gate=gate, clock=clock)
    settings: dict[str, object] = {
        "timeout_ms": 10,
        "ttl_ms": 1_000,
        "base_backoff_ms": 100,
        "max_backoff_ms": 400,
    }
    settings.update(overrides)
    return PredictionRuntime(provider=provider, clock=clock, **settings)  # type: ignore[arg-type]


class PredictionTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_produces_no_prediction(self) -> None:
        clock = FakeClock(now=1_000)
        runtime = _runtime(gate=asyncio.Event(), clock=clock)

        result = await runtime.submit(warm_market_states(1)[0])

        self.assertIs(result.outcome, PredictionOutcome.TIMEOUT)
        self.assertIsNone(result.record)
        self.assertIsNone(result.request and result.record)
        self.assertIsInstance(result.error, PredictionTimeoutError)
        self.assertEqual(runtime.latest_prediction, None)
        self.assertEqual(len(runtime.archive), 0)
        self.assertTrue(all(submission.record is None for submission in runtime.submissions))

    async def test_timeout_never_fabricates_probabilities(self) -> None:
        clock = FakeClock(now=0)
        runtime = _runtime(gate=asyncio.Event(), clock=clock)

        await runtime.submit(warm_market_states(1)[0])

        self.assertIsNone(runtime.latest_prediction)
        self.assertEqual(runtime.archive.records, ())  # type: ignore[attr-defined]

    async def test_timeout_marks_provider_backing_off(self) -> None:
        clock = FakeClock(now=5_000)
        runtime = _runtime(gate=asyncio.Event(), clock=clock)

        await runtime.submit(warm_market_states(1)[0])

        self.assertIs(runtime.provider_status, ProviderStatus.BACKING_OFF)
        self.assertEqual(runtime.consecutive_failures, 1)
        self.assertEqual(runtime.next_retry_at, 5_100)

    async def test_timeout_releases_inflight(self) -> None:
        clock = FakeClock(now=0)
        runtime = _runtime(gate=asyncio.Event(), clock=clock)

        await runtime.submit(warm_market_states(1)[0])

        self.assertEqual(runtime.scheduler.inflight, 0)
        self.assertIsNotNone(runtime.scheduler.reserve())
        runtime.scheduler.abandon()

    async def test_timeout_does_not_leave_latest_prediction_behind(self) -> None:
        clock = FakeClock(now=0)
        good_provider = FakeProvider(clock=clock, latency_ms=1)
        runtime = PredictionRuntime(provider=good_provider, clock=clock, timeout_ms=50, ttl_ms=1_000)
        state = warm_market_states(1)[0]
        accepted = await runtime.submit(state)
        self.assertTrue(accepted.accepted)

        gate = asyncio.Event()
        failing = PredictionRuntime(
            provider=FakeProvider(gate=gate, clock=clock), clock=clock, timeout_ms=10, ttl_ms=1_000
        )
        timeout_result = await failing.submit(state)

        self.assertIs(timeout_result.outcome, PredictionOutcome.TIMEOUT)
        self.assertIsNone(failing.latest_prediction)
        self.assertIsNotNone(runtime.latest_prediction)

    async def test_retry_allowed_after_backoff_window(self) -> None:
        clock = FakeClock(now=0)
        gate = asyncio.Event()
        provider = FakeProvider(gate=gate, clock=clock)
        runtime = PredictionRuntime(
            provider=provider, clock=clock, timeout_ms=10, ttl_ms=1_000, base_backoff_ms=100, max_backoff_ms=400
        )
        state = warm_market_states(1)[0]

        first = await runtime.submit(state)
        self.assertIs(first.outcome, PredictionOutcome.TIMEOUT)
        skipped = await runtime.submit(state)
        self.assertIs(skipped.outcome, PredictionOutcome.SKIPPED_BACKOFF)

        clock.advance(100)
        gate.set()  # 放行：模拟 provider 恢复
        recovered = await runtime.submit(state)

        self.assertIs(recovered.outcome, PredictionOutcome.ACCEPTED)
        self.assertEqual(recovered.record.raw_response, response_json())  # type: ignore[union-attr]
        self.assertIs(runtime.provider_status, ProviderStatus.HEALTHY)


if __name__ == "__main__":
    unittest.main()
