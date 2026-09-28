"""P0001.4.2 集成测试（stub server）：System One → 既有 Runtime → PredictionRecord。

覆盖 SC-1（离线代理链路）、SC-5（证据字段）、SC-7（Key 隔离）与 wire 请求契约。
"""

from __future__ import annotations

import json
import unittest

from prediction.providers.systemone import SystemOneProvider, SystemOneTransport
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import FUTURE_RETURN_HORIZONS_MS, build_jev_payload_json, market_state_hash
from prediction.systemone_wire import SYSTEMONE_MODEL_ALIAS
from prediction.types import PredictionMode, PredictionOutcome, ProviderUsage
from tests.fakes import FakeClock
from tests.stub_server import (
    FAKE_API_KEY,
    FAKE_KEY_MARKER,
    StubOpenRouterServer,
    StubResponse,
    systemone_answers,
    systemone_envelope,
)
from tests.support import warm_market_states

THRESHOLD_BPS = 5.0


class SystemOneProviderChainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.stub = StubOpenRouterServer().start()
        self.addCleanup(self.stub.stop)
        self.stub.set_response(StubResponse(body=systemone_envelope()))

    def _transport(self, *, timeout_s: float = 5.0) -> SystemOneTransport:
        return SystemOneTransport(
            base_url=self.stub.base_url,
            path="/v1/systemone",
            api_key=FAKE_API_KEY,
            timeout_s=timeout_s,
        )

    def _runtime(self, *, mode: PredictionMode = PredictionMode.LIVE_REQUERY, archive=None) -> PredictionRuntime:
        provider = SystemOneProvider(self._transport(), adverse_selection_threshold_bps=THRESHOLD_BPS)
        return PredictionRuntime(
            provider=provider, clock=FakeClock(now=1_000), timeout_ms=5_000, ttl_ms=2_000, mode=mode, archive=archive
        )

    async def test_sc1_warm_market_state_reaches_a_prediction_record(self) -> None:
        state = warm_market_states(1)[0]

        result = await self._runtime().submit(state)

        self.assertIs(result.outcome, PredictionOutcome.ACCEPTED)
        record = result.record
        assert record is not None
        self.assertEqual(record.market_state_hash, market_state_hash(state))
        self.assertEqual(len(record.prediction.future_return), 4)
        for horizon in FUTURE_RETURN_HORIZONS_MS:
            with self.subTest(horizon=horizon):
                distribution = record.prediction.distribution(horizon)
                assert distribution is not None
                self.assertAlmostEqual(distribution.probability_sum, 1.0)
        self.assertEqual(record.prediction.buy_fill_probability, 0.73)
        self.assertEqual(record.prediction.sell_adverse_selection, 0.73)

    async def test_sc5_record_carries_the_provider_evidence(self) -> None:
        state = warm_market_states(1)[0]

        result = await self._runtime().submit(state)

        record = result.record
        assert record is not None
        self.assertEqual(record.requested_model, SYSTEMONE_MODEL_ALIAS)
        self.assertEqual(record.model, SYSTEMONE_MODEL_ALIAS)
        self.assertEqual(record.resolved_model, "typesafe/jev-1.13-20260917")
        self.assertEqual(record.provider, "TypeSafe")
        self.assertEqual(record.response_id, "gen-dec-test-0001")
        self.assertIsInstance(record.usage, ProviderUsage)
        assert record.usage is not None
        self.assertEqual(record.usage.input_tokens, 512)
        self.assertEqual(record.usage.output_tokens, 48)
        self.assertAlmostEqual(record.usage.cost or 0.0, 1.2e-05)
        self.assertGreaterEqual(record.latency_ms, 0)
        self.assertEqual(record.question_schema_version, "jev-market-v1")

    async def test_wire_request_matches_the_confirmed_contract(self) -> None:
        state = warm_market_states(1)[0]

        await self._runtime().submit(state)

        sent = self.stub.requests[0]
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.path, "/api/v1/systemone")
        self.assertEqual(sent.authorization, f"Bearer {FAKE_API_KEY}")
        self.assertEqual(sent.content_type, "application/json")
        assert sent.body is not None
        self.assertEqual(sorted(sent.body), ["model", "questions", "state"])
        self.assertEqual(sent.body["model"], SYSTEMONE_MODEL_ALIAS)
        questions = sent.body["questions"]
        assert isinstance(questions, dict)
        self.assertEqual(sorted(questions), [
            "buy_adverse_selection", "buy_fill", "market_15s", "market_30s", "market_5s", "market_60s",
            "sell_adverse_selection", "sell_fill",
        ])
        self.assertEqual(questions["market_5s"]["type"], "choice")  # type: ignore[index]
        self.assertEqual(questions["buy_fill"]["type"], "noul")  # type: ignore[index]
        state_payload = json.loads(build_jev_payload_json(state))
        self.assertNotIn("questions", sent.body["state"])  # type: ignore[operator]
        self.assertEqual(sent.body["state"]["market_state_hash"], state_payload["market_state_hash"])  # type: ignore[index]

    async def test_telemetry_records_status_latency_and_sizes(self) -> None:
        state = warm_market_states(1)[0]
        provider = SystemOneProvider(self._transport(), adverse_selection_threshold_bps=THRESHOLD_BPS)
        runtime = PredictionRuntime(provider=provider, clock=FakeClock(now=0), timeout_ms=5_000, ttl_ms=1_000)

        await runtime.submit(state)

        calls = provider.transport.calls
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.http_status, 200)
        self.assertTrue(call.succeeded)
        self.assertGreater(call.request_bytes, 0)
        self.assertGreater(call.response_bytes, 0)
        self.assertGreaterEqual(call.latency_ms, 0)
        self.assertEqual(call.model, SYSTEMONE_MODEL_ALIAS)
        self.assertEqual(call.endpoint, self.stub.systemone_endpoint)

    async def test_sc7_api_key_never_enters_telemetry_or_record(self) -> None:
        state = warm_market_states(1)[0]
        provider = SystemOneProvider(self._transport(), adverse_selection_threshold_bps=THRESHOLD_BPS)
        runtime = PredictionRuntime(provider=provider, clock=FakeClock(now=0), timeout_ms=5_000, ttl_ms=1_000)

        result = await runtime.submit(state)

        assert result.record is not None
        surface = json.dumps(
            {
                "telemetry": [call.as_dict() for call in provider.transport.calls],
                "request_body": provider.transport.calls[0].request_body,
                "record": repr(result.record),
                "raw_response": result.record.raw_response,
            }
        )
        self.assertNotIn(FAKE_KEY_MARKER, surface)
        self.assertEqual(self.stub.requests[0].authorization, f"Bearer {FAKE_API_KEY}")

    async def test_recorded_mode_does_not_touch_the_network(self) -> None:
        state = warm_market_states(1)[0]
        live = self._runtime()
        first = await live.submit(state)
        self.assertTrue(first.accepted)

        recorded = self._runtime(mode=PredictionMode.RECORDED, archive=live.archive)
        before = len(self.stub.requests)
        result = await recorded.submit(state)

        self.assertTrue(result.accepted)
        self.assertEqual(len(self.stub.requests), before)
        self.assertEqual(result.record, first.record)

    async def test_repeated_state_keeps_first_archived_record(self) -> None:
        state = warm_market_states(1)[0]
        runtime = self._runtime()

        first = await runtime.submit(state)
        second = await runtime.submit(state)

        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertEqual(len(self.stub.requests), 2)
        self.assertEqual(
            runtime.archive.find(market_state_hash=market_state_hash(state), question_schema_version="jev-market-v1"),
            first.record,
        )

    async def test_stub_answers_are_passed_through_unchanged(self) -> None:
        probabilities = {"strong_down": 0.02, "down": 0.18, "flat": 0.58, "up": 0.19, "strong_up": 0.03}
        self.stub.set_response(StubResponse(body=systemone_envelope(systemone_answers(choice_probabilities=probabilities))))

        result = await self._runtime().submit(warm_market_states(1)[0])

        record = result.record
        assert record is not None
        distribution = record.prediction.distribution(5_000)
        assert distribution is not None
        for category, value in probabilities.items():
            with self.subTest(category=category):
                self.assertAlmostEqual(dict(distribution.as_mapping())[category], value)


if __name__ == "__main__":
    unittest.main()
