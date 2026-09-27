"""SC-5：TTL 是一等字段。"""

from __future__ import annotations

import unittest

from prediction.runtime import PredictionRuntime
from prediction.types import PredictionMode
from tests.fakes import FakeClock, FakeProvider
from tests.support import warm_market_states


def _runtime(*, ttl_ms: int = 2_000, latency_ms: int = 25) -> tuple[PredictionRuntime, FakeClock]:
    clock = FakeClock(now=1_700_000_000_000)
    runtime = PredictionRuntime(
        provider=FakeProvider(clock=clock, latency_ms=latency_ms),
        clock=clock,
        timeout_ms=500,
        ttl_ms=ttl_ms,
    )
    return runtime, clock


class PredictionTtlTest(unittest.IsolatedAsyncioTestCase):
    async def test_record_carries_full_ttl_metadata(self) -> None:
        runtime, clock = _runtime()
        result = await runtime.submit(warm_market_states(1)[0])

        record = result.record
        assert record is not None
        self.assertEqual(record.request_created_at, clock.now() - 25)
        self.assertEqual(record.response_received_at, clock.now())
        self.assertEqual(record.latency_ms, 25)
        self.assertEqual(record.expires_at, record.request_created_at + 2_000)
        self.assertEqual(record.as_of, warm_market_states(1)[0].time.as_of_exchange_ts)

    async def test_expiry_boundary_is_explicit(self) -> None:
        runtime, clock = _runtime()
        record = (await runtime.submit(warm_market_states(1)[0])).record
        assert record is not None

        self.assertFalse(record.is_expired(record.expires_at - 1))
        self.assertFalse(record.is_expired(record.expires_at))
        self.assertTrue(record.is_expired(record.expires_at + 1))

    async def test_ttl_remaining_counts_down(self) -> None:
        runtime, clock = _runtime()
        record = (await runtime.submit(warm_market_states(1)[0])).record
        assert record is not None

        self.assertEqual(record.ttl_remaining_ms(record.request_created_at), 2_000)
        self.assertEqual(record.ttl_remaining_ms(record.request_created_at + 1_500), 500)
        self.assertEqual(record.ttl_remaining_ms(record.expires_at), 0)
        self.assertEqual(record.ttl_remaining_ms(record.expires_at + 10_000), 0)

    async def test_runtime_reports_expiry_against_its_clock(self) -> None:
        runtime, clock = _runtime()
        await runtime.submit(warm_market_states(1)[0])
        record = runtime.latest_prediction
        assert record is not None

        self.assertFalse(runtime.is_expired(record))
        clock.advance(2_001)
        self.assertTrue(runtime.is_expired(record))

    async def test_ttl_does_not_gate_acceptance(self) -> None:
        # 本阶段只建立 TTL 契约，不做交易 Gate：消费方负责检查 now <= expires_at
        runtime, clock = _runtime(ttl_ms=1, latency_ms=50)
        result = await runtime.submit(warm_market_states(1)[0])

        self.assertTrue(result.accepted)
        record = result.record
        assert record is not None
        self.assertTrue(runtime.is_expired(record))

    async def test_invalid_ttl_configuration_rejected(self) -> None:
        provider = FakeProvider()
        clock = FakeClock()
        for ttl_ms in (0, -1):
            with self.subTest(ttl_ms=ttl_ms):
                with self.assertRaises(ValueError):
                    PredictionRuntime(provider=provider, clock=clock, timeout_ms=100, ttl_ms=ttl_ms)

    async def test_recorded_mode_keeps_original_ttl(self) -> None:
        runtime, _ = _runtime()
        state = warm_market_states(1)[0]
        live = await runtime.submit(state)
        assert live.record is not None

        recorded = PredictionRuntime(
            provider=FakeProvider(error=AssertionError("must not be called")),
            clock=FakeClock(now=1_700_000_100_000),
            timeout_ms=100,
            ttl_ms=999,
            mode=PredictionMode.RECORDED,
            archive=runtime.archive,
        )
        result = await recorded.submit(state)

        assert result.record is not None
        self.assertEqual(result.record.expires_at, live.record.expires_at)
        self.assertEqual(result.record.mode, PredictionMode.LIVE_REQUERY)


if __name__ == "__main__":
    unittest.main()
