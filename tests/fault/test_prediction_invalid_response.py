"""SC-4 / SC-7：非法响应与 provider 失败一律 fail closed。"""

from __future__ import annotations

import json
import unittest

from prediction.errors import (
    PredictionInvalidResponseError,
    PredictionProviderError,
    PredictionTransportError,
)
from prediction.providers.jev import JevProvider
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import QUESTION_SCHEMA_VERSION
from prediction.types import PredictionOutcome
from tests.fakes import FakeClock, FakeProvider, response_json, response_payload
from tests.support import warm_market_states


def _mutated(**mutations: object) -> str:
    payload = json.loads(json.dumps(response_payload()))
    for key, value in mutations.items():
        if key == "delete":
            del payload[value]  # type: ignore[arg-type]
        elif key.startswith("set_"):
            payload[key[4:]] = value
        elif key == "nested":
            path, nested_value = value  # type: ignore[misc]
            target = payload
            *parents, leaf = path
            for parent in parents:
                target = target[parent]
            target[leaf] = nested_value
        else:  # pragma: no cover - 测试用法错误
            raise AssertionError(f"unknown mutation {key}")
    return json.dumps(payload, separators=(",", ":"))


class InvalidResponseTest(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, raw_response: str | None = None, error: BaseException | None = None) -> PredictionRuntime:
        clock = FakeClock(now=0)
        provider = FakeProvider(raw_response=raw_response, error=error, clock=clock)
        return PredictionRuntime(provider=provider, clock=clock, timeout_ms=100, ttl_ms=1_000)

    async def _assert_rejected(self, runtime: PredictionRuntime, expected: PredictionOutcome) -> None:
        result = await runtime.submit(warm_market_states(1)[0])

        self.assertIs(result.outcome, expected)
        self.assertIsNone(result.record)
        self.assertIsNotNone(result.error)
        self.assertIsNone(runtime.latest_prediction)
        self.assertEqual(len(runtime.archive), 0)
        self.assertEqual(runtime.scheduler.inflight, 0)

    async def test_malformed_json_is_parse_error(self) -> None:
        await self._assert_rejected(self._runtime(raw_response="{not json"), PredictionOutcome.PARSE_ERROR)

    async def test_non_object_response_is_parse_error(self) -> None:
        await self._assert_rejected(self._runtime(raw_response="[1,2,3]"), PredictionOutcome.PARSE_ERROR)

    async def test_empty_response_is_invalid(self) -> None:
        await self._assert_rejected(self._runtime(raw_response=""), PredictionOutcome.INVALID_RESPONSE)

    async def test_wrong_schema_version_is_invalid(self) -> None:
        raw = response_json(question_schema_version="jev-market-v2")
        await self._assert_rejected(self._runtime(raw_response=raw), PredictionOutcome.INVALID_RESPONSE)

    async def test_missing_category_is_invalid(self) -> None:
        raw = _mutated(nested=(("future_return", "5s"), {"strong_down": 0.2, "down": 0.2, "up": 0.3, "strong_up": 0.3}))
        await self._assert_rejected(self._runtime(raw_response=raw), PredictionOutcome.INVALID_RESPONSE)

    async def test_out_of_range_probability_is_invalid(self) -> None:
        raw = _mutated(set_buy_fill_probability=1.5)
        await self._assert_rejected(self._runtime(raw_response=raw), PredictionOutcome.INVALID_RESPONSE)

    async def test_negative_probability_is_invalid(self) -> None:
        raw = _mutated(set_sell_adverse_selection=-0.01)
        await self._assert_rejected(self._runtime(raw_response=raw), PredictionOutcome.INVALID_RESPONSE)

    async def test_probability_sum_violation_is_invalid(self) -> None:
        raw = _mutated(nested=(("future_return", "15s"), {"strong_down": 0.5, "down": 0.5, "flat": 0.5, "up": 0.0, "strong_up": 0.0}))
        await self._assert_rejected(self._runtime(raw_response=raw), PredictionOutcome.INVALID_RESPONSE)

    async def test_unknown_field_is_invalid(self) -> None:
        raw = _mutated(set_extra_signal=0.5)
        await self._assert_rejected(self._runtime(raw_response=raw), PredictionOutcome.INVALID_RESPONSE)

    async def test_nan_is_invalid(self) -> None:
        raw = response_json(flat=float("nan"))
        self.assertIn("NaN", raw)
        await self._assert_rejected(self._runtime(raw_response=raw), PredictionOutcome.INVALID_RESPONSE)

    async def test_infinity_is_invalid(self) -> None:
        raw = response_json(flat=float("inf"))
        self.assertIn("Infinity", raw)
        await self._assert_rejected(self._runtime(raw_response=raw), PredictionOutcome.INVALID_RESPONSE)

    async def test_declared_transport_error_is_mapped(self) -> None:
        runtime = self._runtime(error=PredictionTransportError("connection refused"))
        await self._assert_rejected(runtime, PredictionOutcome.TRANSPORT_ERROR)

    async def test_declared_provider_error_is_mapped(self) -> None:
        runtime = self._runtime(error=PredictionProviderError("model timeout upstream"))
        await self._assert_rejected(runtime, PredictionOutcome.PROVIDER_ERROR)

    async def test_unexpected_provider_exception_is_provider_error(self) -> None:
        runtime = self._runtime(error=RuntimeError("boom"))
        await self._assert_rejected(runtime, PredictionOutcome.PROVIDER_ERROR)

    async def test_failures_are_logged_with_errors(self) -> None:
        runtime = self._runtime(error=RuntimeError("boom"))
        await runtime.submit(warm_market_states(1)[0])

        self.assertEqual(len(runtime.submissions), 1)
        self.assertIsInstance(runtime.submissions[0].error, PredictionProviderError)


class JevProviderBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_transport_exception_maps_to_transport_error(self) -> None:
        async def transport(request: object) -> str:
            raise ConnectionError("refused")

        provider = JevProvider(transport, model="jev-x")  # type: ignore[arg-type]
        with self.assertRaises(PredictionTransportError):
            await provider.predict(_request())

    async def test_declared_errors_pass_through(self) -> None:
        async def transport(request: object) -> str:
            raise PredictionProviderError("upstream")

        provider = JevProvider(transport, model="jev-x")  # type: ignore[arg-type]
        with self.assertRaises(PredictionProviderError):
            await provider.predict(_request())

    async def test_empty_transport_response_is_invalid(self) -> None:
        async def transport(request: object) -> str:
            return "   "

        provider = JevProvider(transport, model="jev-x")  # type: ignore[arg-type]
        with self.assertRaises(PredictionInvalidResponseError):
            await provider.predict(_request())

    async def test_valid_transport_returns_metadata(self) -> None:
        async def transport(request: object) -> str:
            return response_json(confidence=0.4)

        provider = JevProvider(transport, model="jev-x")  # type: ignore[arg-type]
        response = await provider.predict(_request())

        self.assertEqual(response.provider, JevProvider.PROVIDER_NAME)
        self.assertEqual(response.model, "jev-x")
        self.assertEqual(response.raw_response, response_json(confidence=0.4))

    async def test_request_payload_is_what_the_transport_receives(self) -> None:
        seen: list[str] = []

        async def transport(request) -> str:  # type: ignore[no-untyped-def]
            seen.append(request.payload_json)
            return response_json()

        provider = JevProvider(transport, model="jev-x")
        request = _request()
        await provider.predict(request)

        self.assertEqual(seen, [request.payload_json])

    def test_invalid_configuration_rejected(self) -> None:
        with self.assertRaises(ValueError):
            JevProvider(None, model="m")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            JevProvider(lambda request: None, model="")  # type: ignore[arg-type,return-value]


def _request():
    from prediction.schema.market_v1 import build_prediction_request

    return build_prediction_request(warm_market_states(1)[0], sequence=0, created_at=0, ttl_ms=1_000)


if __name__ == "__main__":
    unittest.main()
