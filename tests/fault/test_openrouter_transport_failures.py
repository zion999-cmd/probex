"""SC-3 / SC-4：OpenRouter 失败路径必须映射为既有 failure type，且不泄漏 Key。

全部使用本机 stub server 或不可达端口（不访问公网）。
"""

from __future__ import annotations

import json
import unittest

from prediction.errors import (
    PredictionInvalidResponseError,
    PredictionProviderError,
    PredictionTimeoutError,
    PredictionTransportError,
)
from prediction.providers.jev import JevProvider
from prediction.providers.openrouter import OPENROUTER_MODEL, OpenRouterTransport
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import build_prediction_request
from prediction.types import PredictionOutcome
from tests.fakes import FakeClock, response_json
from tests.stub_server import (
    FAKE_API_KEY,
    FAKE_KEY_MARKER,
    StubOpenRouterServer,
    StubResponse,
    openrouter_envelope,
)
from tests.support import bypass_proxy_for_localhost, warm_market_states


def _request():
    return build_prediction_request(warm_market_states(1)[0], sequence=0, created_at=0, ttl_ms=1_000)


class OpenRouterHttpStatusTest(unittest.IsolatedAsyncioTestCase):
    """非 2xx 状态映射（stub server）。"""

    def setUp(self) -> None:
        super().setUp()
        self.stub = StubOpenRouterServer().start()
        self.addCleanup(self.stub.stop)
        self.transport = OpenRouterTransport(endpoint=self.stub.endpoint, api_key=FAKE_API_KEY, timeout_s=5.0)

    async def _call_with_status(self, status: int, body: str = ""):
        self.stub.set_response(StubResponse(status=status, body=body))
        self.transport.clear_calls()
        return await self.transport(_request())

    async def test_client_errors_map_to_transport_error(self) -> None:
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                with self.assertRaises(PredictionTransportError) as context:
                    await self._call_with_status(status, '{"error":{"message":"bad request"}}')
                self.assertIn(str(status), str(context.exception))

    async def test_rate_limit_and_server_errors_map_to_provider_error(self) -> None:
        for status in (408, 429, 500, 502, 503):
            with self.subTest(status=status):
                with self.assertRaises(PredictionProviderError) as context:
                    await self._call_with_status(status, '{"error":{"message":"upstream"}}')
                self.assertIn(str(status), str(context.exception))

    async def test_status_and_body_are_recorded_in_telemetry(self) -> None:
        with self.assertRaises(PredictionProviderError):
            await self._call_with_status(503, '{"error":{"message":"upstream"}}')

        call = self.transport.calls[-1]
        self.assertEqual(call.http_status, 503)
        self.assertEqual(call.error, PredictionProviderError.__name__)
        self.assertIn("upstream", call.response_body)
        self.assertEqual(call.content, "")

    async def test_missing_api_key_never_reaches_the_network(self) -> None:
        transport = OpenRouterTransport(
            endpoint=self.stub.endpoint, api_key=None, api_key_env="PROBEX_TEST_MISSING_KEY", timeout_s=5.0
        )
        with self.assertRaises(PredictionTransportError) as context:
            await transport(_request())

        self.assertIn("PROBEX_TEST_MISSING_KEY", str(context.exception))
        self.assertEqual(self.stub.requests, [], "缺 Key 时不得发起 HTTP 请求")
        self.assertEqual(transport.calls[-1].http_status, None)

    async def test_two_thousand_status_is_success(self) -> None:
        content = await self._call_with_status(200, openrouter_envelope('{"ok":1}'))
        self.assertEqual(content, '{"ok":1}')

    async def test_envelope_errors_map_to_invalid_response(self) -> None:
        cases = {
            "not json": "<html>gateway error</html>",
            "no choices": '{"id":"x"}',
            "empty content": '{"choices":[{"message":{"content":""}}]}',
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(PredictionInvalidResponseError):
                    await self._call_with_status(200, body)
                self.assertEqual(self.transport.calls[-1].error, PredictionInvalidResponseError.__name__)


class OpenRouterTimeoutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 「连接被拒绝」必须直连本机，不能被系统代理改写成超时
        bypass_proxy_for_localhost()

    async def test_http_timeout_maps_to_timeout(self) -> None:
        with StubOpenRouterServer() as stub:
            stub.set_response(StubResponse(body=openrouter_envelope("{}"), delay_s=0.5))
            transport = OpenRouterTransport(endpoint=stub.endpoint, api_key=FAKE_API_KEY, timeout_s=0.1)

            with self.assertRaises(PredictionTimeoutError):
                await transport(_request())

            call = transport.calls[-1]
            self.assertEqual(call.error, PredictionTimeoutError.__name__)
            self.assertEqual(call.http_status, None)

    async def test_connection_refused_maps_to_transport_error(self) -> None:
        transport = OpenRouterTransport(
            endpoint="http://127.0.0.1:1/api/v1/chat/completions", api_key=FAKE_API_KEY, timeout_s=1.0
        )
        with self.assertRaises(PredictionTransportError):
            await transport(_request())

        self.assertEqual(transport.calls[-1].error, PredictionTransportError.__name__)

    async def test_runtime_timeout_still_produces_no_record(self) -> None:
        with StubOpenRouterServer() as stub:
            stub.set_response(StubResponse(body=openrouter_envelope(response_json()), delay_s=0.5))
            provider = JevProvider(
                OpenRouterTransport(endpoint=stub.endpoint, api_key=FAKE_API_KEY, timeout_s=5.0),
                model=OPENROUTER_MODEL,
            )
            runtime = PredictionRuntime(provider=provider, clock=FakeClock(), timeout_ms=50, ttl_ms=1_000)

            result = await runtime.submit(warm_market_states(1)[0])

            self.assertIs(result.outcome, PredictionOutcome.TIMEOUT)
            self.assertIsNone(result.record)
            self.assertIsNone(runtime.latest_prediction)
            self.assertEqual(len(runtime.archive), 0)


class OpenRouterFailureThroughRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """SC-3：失败经 runtime 映射为既有 failure type，且不产生假 Prediction。"""

    def setUp(self) -> None:
        super().setUp()
        self.stub = StubOpenRouterServer().start()
        self.addCleanup(self.stub.stop)

    def _runtime(self) -> PredictionRuntime:
        provider = JevProvider(
            OpenRouterTransport(endpoint=self.stub.endpoint, api_key=FAKE_API_KEY, timeout_s=5.0),
            model=OPENROUTER_MODEL,
        )
        return PredictionRuntime(provider=provider, clock=FakeClock(), timeout_ms=5_000, ttl_ms=1_000)

    async def test_failure_outcomes_and_no_records(self) -> None:
        cases = {
            PredictionOutcome.TRANSPORT_ERROR: StubResponse(status=401, body='{"error":"unauthorized"}'),
            PredictionOutcome.PROVIDER_ERROR: StubResponse(status=503, body='{"error":"unavailable"}'),
            PredictionOutcome.INVALID_RESPONSE: StubResponse(status=200, body="not json"),
            PredictionOutcome.PARSE_ERROR: StubResponse(
                status=200, body=openrouter_envelope("{not json inside content")
            ),
            PredictionOutcome.INVALID_RESPONSE: StubResponse(
                status=200, body=openrouter_envelope(json.dumps({"unexpected": True}))
            ),
        }
        for expected, response in cases.items():
            with self.subTest(outcome=expected.name):
                self.stub.set_response(response)
                runtime = self._runtime()
                result = await runtime.submit(warm_market_states(1)[0])

                self.assertIs(result.outcome, expected)
                self.assertIsNone(result.record)
                self.assertIsNotNone(result.error)
                self.assertIsNone(runtime.latest_prediction)
                self.assertEqual(len(runtime.archive), 0)

    async def test_success_after_failure_is_not_blocked_by_backoff_after_window(self) -> None:
        clock = FakeClock(now=0)
        provider = JevProvider(
            OpenRouterTransport(endpoint=self.stub.endpoint, api_key=FAKE_API_KEY, timeout_s=5.0),
            model=OPENROUTER_MODEL,
        )
        runtime = PredictionRuntime(
            provider=provider, clock=clock, timeout_ms=5_000, ttl_ms=1_000, base_backoff_ms=100, max_backoff_ms=400
        )
        state = warm_market_states(1)[0]

        self.stub.set_response(StubResponse(status=500, body="boom"))
        failed = await runtime.submit(state)
        self.assertIs(failed.outcome, PredictionOutcome.PROVIDER_ERROR)

        skipped = await runtime.submit(state)
        self.assertIs(skipped.outcome, PredictionOutcome.SKIPPED_BACKOFF)

        clock.advance(100)
        self.stub.set_response(StubResponse(status=200, body=openrouter_envelope(response_json())))
        recovered = await runtime.submit(state)

        self.assertIs(recovered.outcome, PredictionOutcome.ACCEPTED)
        self.assertIsNotNone(recovered.record)


class ApiKeyRedactionTest(unittest.IsolatedAsyncioTestCase):
    """SC-4：Key 不得出现在异常文本、telemetry、Record 或任何落盘 fixture。"""

    def setUp(self) -> None:
        super().setUp()
        self.stub = StubOpenRouterServer().start()
        self.addCleanup(self.stub.stop)
        self.transport = OpenRouterTransport(endpoint=self.stub.endpoint, api_key=FAKE_API_KEY, timeout_s=5.0)

    async def test_key_absent_from_every_failure_path(self) -> None:
        failures: list[BaseException] = []
        for response in (
            StubResponse(status=401, body='{"error":"unauthorized"}'),
            StubResponse(status=429, body='{"error":"rate limited"}'),
            StubResponse(status=200, body="<html>not json</html>"),
            StubResponse(status=200, body=openrouter_envelope("{broken")),
        ):
            self.stub.set_response(response)
            try:
                await self.transport(_request())
            except BaseException as exc:  # noqa: BLE001 - 记录全部失败文本用于断言
                failures.append(exc)

        self.assertTrue(failures)
        for exc in failures:
            self.assertNotIn(FAKE_KEY_MARKER, str(exc), "Key 不得出现在异常文本")
            self.assertNotIn(FAKE_KEY_MARKER, repr(exc), "Key 不得出现在异常 repr")

    async def test_key_absent_from_telemetry_and_record(self) -> None:
        self.stub.set_response(StubResponse(status=200, body=openrouter_envelope(response_json())))
        provider = JevProvider(self.transport, model=OPENROUTER_MODEL)
        runtime = PredictionRuntime(provider=provider, clock=FakeClock(), timeout_ms=5_000, ttl_ms=1_000)
        result = await runtime.submit(warm_market_states(1)[0])

        assert result.record is not None
        surface = json.dumps(
            {
                "telemetry": [call.as_dict() for call in self.transport.calls],
                "request_body": str(result.record.raw_response),
                "record": repr(result.record),
                "payload": result.request.payload_json if result.request else "",
            },
            default=str,
        )
        self.assertNotIn(FAKE_KEY_MARKER, surface)

    async def test_key_only_appears_in_the_authorization_header(self) -> None:
        self.stub.set_response(StubResponse(status=200, body=openrouter_envelope("{}")))
        await self.transport(_request())

        request = self.stub.requests[-1]
        self.assertEqual(request.authorization, f"Bearer {FAKE_API_KEY}")
        self.assertNotIn(FAKE_KEY_MARKER, request.raw_body)


if __name__ == "__main__":
    unittest.main()
