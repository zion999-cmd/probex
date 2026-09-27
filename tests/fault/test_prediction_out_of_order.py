"""SC-3：逆序返回时旧结果不得覆盖新结果。"""

from __future__ import annotations

import asyncio
import unittest

from prediction.providers.base import ProviderResponse
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import market_state_hash
from prediction.types import PredictionOutcome, PredictionRequest
from tests.fakes import FakeClock, response_json
from tests.support import warm_market_states


class _GatedProvider:
    """按请求序号逐个放行的 provider，用于构造逆序返回。"""

    def __init__(self) -> None:
        self.requests: list[PredictionRequest] = []
        self.gates: dict[int, asyncio.Event] = {}

    async def predict(self, request: PredictionRequest) -> ProviderResponse:
        self.requests.append(request)
        gate = self.gates.setdefault(request.sequence, asyncio.Event())
        await gate.wait()
        return ProviderResponse(provider="gated", model="gated-v1", raw_response=response_json())

    async def wait_for_requests(self, count: int) -> None:
        for _ in range(50):
            if len(self.gates) >= count:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"provider did not receive {count} requests")


class PredictionOutOfOrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_sc3_late_older_response_is_stale(self) -> None:
        states = warm_market_states(2)
        provider = _GatedProvider()
        runtime = PredictionRuntime(
            provider=provider,
            clock=FakeClock(now=1_000),
            timeout_ms=5_000,
            ttl_ms=2_000,
            max_inflight=2,
        )

        task_a = asyncio.create_task(runtime.submit(states[0]))
        await provider.wait_for_requests(1)
        task_b = asyncio.create_task(runtime.submit(states[1]))
        await provider.wait_for_requests(2)

        self.assertEqual([request.sequence for request in provider.requests], [0, 1])

        # B 先返回
        provider.gates[1].set()
        result_b = await task_b
        # A 后返回
        provider.gates[0].set()
        result_a = await task_a

        self.assertIs(result_b.outcome, PredictionOutcome.ACCEPTED)
        self.assertIs(result_a.outcome, PredictionOutcome.STALE_RESPONSE)

        # 迟到结果仍被记录，但绝不能成为 current prediction
        self.assertIsNotNone(result_a.record)
        self.assertIs(runtime.latest_prediction, result_b.record)
        self.assertEqual(runtime.scheduler.stale_count, 1)
        self.assertEqual(runtime.scheduler.accepted_count, 1)
        self.assertEqual(runtime.scheduler.inflight, 0)

        # archive 只收下被接受的那条
        self.assertEqual([record.sequence for record in runtime.archive.records], [1])  # type: ignore[attr-defined]
        self.assertEqual(
            runtime.archive.find(
                market_state_hash=market_state_hash(states[1]), question_schema_version="jev-market-v1"
            ),
            result_b.record,
        )
        self.assertIsNone(
            runtime.archive.find(
                market_state_hash=market_state_hash(states[0]), question_schema_version="jev-market-v1"
            )
        )

    async def test_in_order_responses_are_both_accepted(self) -> None:
        states = warm_market_states(2)
        provider = _GatedProvider()
        runtime = PredictionRuntime(
            provider=provider,
            clock=FakeClock(now=0),
            timeout_ms=5_000,
            ttl_ms=1_000,
            max_inflight=2,
        )

        task_a = asyncio.create_task(runtime.submit(states[0]))
        await provider.wait_for_requests(1)
        task_b = asyncio.create_task(runtime.submit(states[1]))
        await provider.wait_for_requests(2)

        provider.gates[0].set()
        result_a = await task_a
        provider.gates[1].set()
        result_b = await task_b

        self.assertIs(result_a.outcome, PredictionOutcome.ACCEPTED)
        self.assertIs(result_b.outcome, PredictionOutcome.ACCEPTED)
        self.assertEqual(runtime.scheduler.stale_count, 0)
        self.assertIs(runtime.latest_prediction, result_b.record)

    async def test_inflight_limit_skips_instead_of_queueing(self) -> None:
        states = warm_market_states(2)
        provider = _GatedProvider()
        runtime = PredictionRuntime(
            provider=provider,
            clock=FakeClock(now=0),
            timeout_ms=5_000,
            ttl_ms=1_000,
            max_inflight=1,
        )

        task_a = asyncio.create_task(runtime.submit(states[0]))
        await provider.wait_for_requests(1)
        result_b = await runtime.submit(states[1])

        self.assertIs(result_b.outcome, PredictionOutcome.SKIPPED_INFLIGHT)
        self.assertIsNone(result_b.record)
        self.assertEqual(len(provider.requests), 1, "被跳过的请求不得打到 provider")

        provider.gates[0].set()
        result_a = await task_a
        self.assertTrue(result_a.accepted)


if __name__ == "__main__":
    unittest.main()
