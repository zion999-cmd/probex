"""P0001.4.2 故障测试：System One 失败路径必须 fail closed（SC-6 / SC-7）。

全部使用本机 stub server 或不可达端口（不访问公网），路由经过既有 Runtime，
以证明失败被映射为既有 failure type 且不产生伪 Prediction。
"""

from __future__ import annotations

import json
import unittest

from prediction.errors import PredictionInvalidResponseError, PredictionParseError, PredictionTransportError
from prediction.providers.systemone import SystemOneProvider, SystemOneTransport
from prediction.runtime import PredictionRuntime
from prediction.types import PredictionOutcome
from tests.fakes import FakeClock
from tests.stub_server import (
    FAKE_API_KEY,
    FAKE_KEY_MARKER,
    StubOpenRouterServer,
    StubResponse,
    systemone_answers,
    systemone_envelope,
)
from tests.support import bypass_proxy_for_localhost, warm_market_states

THRESHOLD_BPS = 5.0


class SystemOneHttpFailureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 「连接被拒绝」必须直连本机，不能被系统代理改写成超时
        bypass_proxy_for_localhost()
        self.stub = StubOpenRouterServer().start()
        self.addCleanup(self.stub.stop)

    def _runtime(self) -> PredictionRuntime:
        transport = SystemOneTransport(
            base_url=self.stub.base_url, path="/v1/systemone", api_key=FAKE_API_KEY, timeout_s=5.0
        )
        provider = SystemOneProvider(transport, adverse_selection_threshold_bps=THRESHOLD_BPS)
        return PredictionRuntime(provider=provider, clock=FakeClock(now=0), timeout_ms=5_000, ttl_ms=1_000)

    async def _submit_with(self, response: StubResponse) -> object:
        self.stub.set_response(response)
        return await self._runtime().submit(warm_market_states(1)[0])

    async def test_client_errors_map_to_transport_error(self) -> None:
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                result = await self._submit_with(StubResponse(status=status, body='{"error":{"message":"bad"}}'))
                self.assertIs(result.outcome, PredictionOutcome.TRANSPORT_ERROR)  # type: ignore[attr-defined]
                self.assertIsNone(result.record)  # type: ignore[attr-defined]

    async def test_rate_limit_overload_and_server_errors_map_to_provider_error(self) -> None:
        # 429 / 529（TypeSafe Overloaded）按文档应退避重试 → 交给上层 backoff
        for status in (408, 425, 429, 500, 502, 503, 529):
            with self.subTest(status=status):
                result = await self._submit_with(StubResponse(status=status, body='{"error":{"message":"busy"}}'))
                self.assertIs(result.outcome, PredictionOutcome.PROVIDER_ERROR)  # type: ignore[attr-defined]
                self.assertIsNone(result.record)  # type: ignore[attr-defined]

    async def test_invalid_answers_are_rejected_without_a_record(self) -> None:
        answers = systemone_answers()
        del answers["market_30s"]
        result = await self._submit_with(StubResponse(body=systemone_envelope(answers)))

        self.assertIs(result.outcome, PredictionOutcome.INVALID_RESPONSE)  # type: ignore[attr-defined]
        self.assertIsNone(result.record)  # type: ignore[attr-defined]
        self.assertIsInstance(result.error, PredictionInvalidResponseError)  # type: ignore[attr-defined]

    async def test_non_json_body_is_a_parse_error(self) -> None:
        result = await self._submit_with(StubResponse(body="<html>gateway</html>"))
        self.assertIs(result.outcome, PredictionOutcome.PARSE_ERROR)  # type: ignore[attr-defined]
        self.assertIsInstance(result.error, PredictionParseError)  # type: ignore[attr-defined]

    async def test_http_timeout_maps_to_timeout(self) -> None:
        transport = SystemOneTransport(
            base_url=self.stub.base_url, path="/v1/systemone", api_key=FAKE_API_KEY, timeout_s=0.1
        )
        self.stub.set_response(StubResponse(body=systemone_envelope(), delay_s=0.5))
        runtime = PredictionRuntime(
            provider=SystemOneProvider(transport, adverse_selection_threshold_bps=THRESHOLD_BPS),
            clock=FakeClock(now=0),
            timeout_ms=5_000,
            ttl_ms=1_000,
        )

        result = await runtime.submit(warm_market_states(1)[0])

        self.assertIs(result.outcome, PredictionOutcome.TIMEOUT)
        self.assertIsNone(result.record)

    async def test_connection_refused_maps_to_transport_error(self) -> None:
        transport = SystemOneTransport(
            base_url="http://127.0.0.1:1", path="/v1/systemone", api_key=FAKE_API_KEY, timeout_s=1.0
        )
        runtime = PredictionRuntime(
            provider=SystemOneProvider(transport, adverse_selection_threshold_bps=THRESHOLD_BPS),
            clock=FakeClock(now=0),
            timeout_ms=5_000,
            ttl_ms=1_000,
        )

        result = await runtime.submit(warm_market_states(1)[0])

        self.assertIs(result.outcome, PredictionOutcome.TRANSPORT_ERROR)
        self.assertIsNone(result.record)

    async def test_missing_api_key_never_reaches_the_network(self) -> None:
        transport = SystemOneTransport(
            base_url=self.stub.base_url,
            path="/v1/systemone",
            api_key=None,
            api_key_env="PROBEX_TEST_MISSING_KEY",
            timeout_s=5.0,
        )
        runtime = PredictionRuntime(
            provider=SystemOneProvider(transport, adverse_selection_threshold_bps=THRESHOLD_BPS),
            clock=FakeClock(now=0),
            timeout_ms=5_000,
            ttl_ms=1_000,
        )

        result = await runtime.submit(warm_market_states(1)[0])

        self.assertIs(result.outcome, PredictionOutcome.TRANSPORT_ERROR)
        self.assertEqual(self.stub.requests, [], "缺 Key 时不得发起请求")
        self.assertEqual(transport.calls[-1].http_status, None)

    async def test_failures_are_logged_and_never_produce_a_prediction(self) -> None:
        for response in (
            StubResponse(status=401, body='{"error":"unauthorized"}'),
            StubResponse(status=529, body='{"error":"overloaded"}'),
            StubResponse(body="not json"),
        ):
            with self.subTest(status=response.status):
                runtime = self._runtime()
                self.stub.set_response(response)
                result = await runtime.submit(warm_market_states(1)[0])

                self.assertIsNone(result.record)
                self.assertIsNone(runtime.latest_prediction)
                self.assertEqual(len(runtime.archive), 0)  # type: ignore[arg-type]
                self.assertIsNotNone(result.error)


class SystemOneKeyRedactionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.stub = StubOpenRouterServer().start()
        self.addCleanup(self.stub.stop)

    async def test_key_absent_from_every_failure_surface(self) -> None:
        transport = SystemOneTransport(
            base_url=self.stub.base_url, path="/v1/systemone", api_key=FAKE_API_KEY, timeout_s=5.0
        )
        failures: list[BaseException] = []
        for response in (
            StubResponse(status=401, body='{"error":"unauthorized"}'),
            StubResponse(status=529, body='{"error":"overloaded"}'),
            StubResponse(body="<html>not json</html>"),
        ):
            self.stub.set_response(response)
            runtime = PredictionRuntime(
                provider=SystemOneProvider(transport, adverse_selection_threshold_bps=THRESHOLD_BPS),
                clock=FakeClock(now=0),
                timeout_ms=5_000,
                ttl_ms=1_000,
            )
            result = await runtime.submit(warm_market_states(1)[0])
            if result.error is not None:
                failures.append(result.error)

        self.assertTrue(failures)
        for exc in failures:
            self.assertNotIn(FAKE_KEY_MARKER, str(exc))
            self.assertNotIn(FAKE_KEY_MARKER, repr(exc))
        for call in transport.calls:
            surface = json.dumps(call.as_dict()) + call.request_body + call.response_body
            self.assertNotIn(FAKE_KEY_MARKER, surface)

    async def test_key_only_in_the_authorization_header(self) -> None:
        transport = SystemOneTransport(
            base_url=self.stub.base_url, path="/v1/systemone", api_key=FAKE_API_KEY, timeout_s=5.0
        )
        self.stub.set_response(StubResponse(body=systemone_envelope()))
        runtime = PredictionRuntime(
            provider=SystemOneProvider(transport, adverse_selection_threshold_bps=THRESHOLD_BPS),
            clock=FakeClock(now=0),
            timeout_ms=5_000,
            ttl_ms=1_000,
        )

        await runtime.submit(warm_market_states(1)[0])

        request = self.stub.requests[-1]
        self.assertEqual(request.authorization, f"Bearer {FAKE_API_KEY}")
        self.assertNotIn(FAKE_KEY_MARKER, request.raw_body)

    async def test_missing_key_error_names_the_env_var_only(self) -> None:
        transport = SystemOneTransport(
            base_url=self.stub.base_url, path="/v1/systemone", api_key=None, api_key_env="PROBEX_TEST_MISSING_KEY"
        )
        with self.assertRaises(PredictionTransportError) as context:
            await transport.post("{}", model="jev-1.13")
        self.assertIn("PROBEX_TEST_MISSING_KEY", str(context.exception))


if __name__ == "__main__":
    unittest.main()
