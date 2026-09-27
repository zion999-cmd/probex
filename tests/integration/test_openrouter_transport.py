"""OpenRouter transport：端到端（stub server）与请求契约验证。

SC-1 / SC-2 的离线代理：本机 stub 返回 OpenRouter 外层 + Jev content，
验证分层 `OpenRouterTransport → message.content → JevProvider → strict parser → PredictionRecord`
在无网络、无真实凭证的情况下完整跑通。
"""

from __future__ import annotations

import asyncio
import json
import logging
import unittest

from prediction.providers.jev import JevProvider
from prediction.providers.openrouter import (
    OPENROUTER_MODEL,
    OpenRouterTransport,
    build_chat_completions_body,
    extract_message_content,
)
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import (
    build_jev_payload_json,
    build_prediction_request,
    market_state_hash,
)
from prediction.types import PredictionMode, PredictionOutcome
from tests.fakes import FakeClock, response_json
from tests.stub_server import (
    FAKE_API_KEY,
    FAKE_KEY_MARKER,
    StubOpenRouterServer,
    StubResponse,
    openrouter_envelope,
)
from tests.support import warm_market_states


class OpenRouterEnvelopeTest(unittest.TestCase):
    def test_extracts_message_content(self) -> None:
        self.assertEqual(extract_message_content(openrouter_envelope('{"a":1}')), '{"a":1}')

    def test_extracts_multiline_and_unicode_content(self) -> None:
        content = '{\n  "market_5s": {"choice": "flat", "confidence": 0.8}\n}\n附带中文'
        self.assertEqual(extract_message_content(openrouter_envelope(content)), content)

    def test_invalid_envelopes_rejected(self) -> None:
        cases = {
            "empty": "",
            "not json": "not json at all",
            "json scalar": "42",
            "json array": "[1,2,3]",
            "no choices": '{"id":"x"}',
            "empty choices": '{"choices":[]}',
            "choices not list": '{"choices":{}}',
            "choice not object": '{"choices":["x"]}',
            "no message": '{"choices":[{"index":0}]}',
            "message not object": '{"choices":[{"message":"text"}]}',
            "content missing": '{"choices":[{"message":{"role":"assistant"}}]}',
            "content not string": '{"choices":[{"message":{"content":{}}}]}',
            "content blank": '{"choices":[{"message":{"content":"   "}}]}',
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(Exception) as context:
                    extract_message_content(body)
                self.assertIn(type(context.exception).__name__, {"PredictionInvalidResponseError"})


class OpenRouterRequestBodyTest(unittest.TestCase):
    def test_body_matches_confirmed_contract(self) -> None:
        state = warm_market_states(1)[0]
        request = build_prediction_request(state, sequence=0, created_at=0, ttl_ms=1_000)
        body = json.loads(build_chat_completions_body(request))

        self.assertEqual(set(body), {"model", "messages"})
        self.assertEqual(body["model"], OPENROUTER_MODEL)
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertEqual(body["messages"][0]["content"], request.payload_json)
        self.assertEqual(body["messages"][0]["content"], build_jev_payload_json(state))

    def test_body_is_canonical_json(self) -> None:
        state = warm_market_states(1)[0]
        request = build_prediction_request(state, sequence=0, created_at=0, ttl_ms=1_000)
        body = build_chat_completions_body(request)
        self.assertEqual(body, json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False))


class OpenRouterTransportStubTest(unittest.IsolatedAsyncioTestCase):
    """用本机 stub 验证 transport ↔ OpenRouter envelope ↔ JevProvider 分层。"""

    def setUp(self) -> None:
        super().setUp()
        self.stub = StubOpenRouterServer().start()
        self.addCleanup(self.stub.stop)

    def _transport(self, *, timeout_s: float = 5.0) -> OpenRouterTransport:
        return OpenRouterTransport(
            endpoint=self.stub.endpoint,
            api_key=FAKE_API_KEY,
            timeout_s=timeout_s,
        )

    async def test_transport_returns_message_content(self) -> None:
        self.stub.set_response(StubResponse(body=openrouter_envelope('{"ok":true}')))
        transport = self._transport()

        content = await transport(await self._request())

        self.assertEqual(content, '{"ok":true}')
        self.assertEqual(len(self.stub.requests), 1)

    async def test_request_contract_is_sent_over_the_wire(self) -> None:
        self.stub.set_response(StubResponse(body=openrouter_envelope("{}")))
        transport = self._transport()
        request = await self._request()

        await transport(request)

        sent = self.stub.requests[0]
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.path, "/api/v1/chat/completions")
        self.assertEqual(sent.authorization, f"Bearer {FAKE_API_KEY}")
        self.assertEqual(sent.content_type, "application/json")
        assert sent.body is not None
        self.assertEqual(sent.body["model"], OPENROUTER_MODEL)
        self.assertEqual(sent.body["messages"][0]["content"], request.payload_json)

    async def test_telemetry_records_request_response_and_latency(self) -> None:
        body = openrouter_envelope('{"ok":true}')
        self.stub.set_response(StubResponse(body=body))
        transport = self._transport()

        await transport(await self._request())

        calls = transport.calls
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.http_status, 200)
        self.assertIsNone(call.error)
        self.assertEqual(call.response_body, body)
        self.assertEqual(call.content, '{"ok":true}')
        self.assertEqual(call.response_bytes, len(body.encode("utf-8")))
        self.assertEqual(call.content_bytes, len(b'{"ok":true}'))
        self.assertGreater(call.request_bytes, 0)
        self.assertGreaterEqual(call.latency_ms, 0)
        self.assertEqual(call.endpoint, self.stub.endpoint)
        self.assertEqual(call.model, OPENROUTER_MODEL)
        self.assertIn("jev-market-v1", call.request_body)

    async def test_api_key_never_enters_telemetry(self) -> None:
        self.stub.set_response(StubResponse(body=openrouter_envelope("{}")))
        transport = self._transport()

        await transport(await self._request())

        for call in transport.calls:
            self.assertNotIn(FAKE_KEY_MARKER, json.dumps(call.as_dict()))
            self.assertNotIn(FAKE_KEY_MARKER, call.request_body)
            self.assertNotIn(FAKE_KEY_MARKER, call.response_body)

    async def test_shared_transport_handles_sequential_calls(self) -> None:
        self.stub.set_response(StubResponse(body=openrouter_envelope("{}")))
        transport = self._transport()
        states = warm_market_states(2)

        for state, sequence in zip(states, (0, 1)):
            request = build_prediction_request(state, sequence=sequence, created_at=0, ttl_ms=1_000)
            await transport(request)

        self.assertEqual(len(self.stub.requests), 2)
        self.assertEqual(len(transport.calls), 2)
        self.assertNotEqual(transport.calls[0].request_body, transport.calls[1].request_body)

    async def _request(self):
        return build_prediction_request(warm_market_states(1)[0], sequence=0, created_at=0, ttl_ms=1_000)


class OpenRouterFullChainTest(unittest.IsolatedAsyncioTestCase):
    """分层验证：transport → JevProvider → runtime → PredictionRecord。"""

    def setUp(self) -> None:
        super().setUp()
        self.stub = StubOpenRouterServer().start()
        self.addCleanup(self.stub.stop)
        self.provider = JevProvider(
            OpenRouterTransport(endpoint=self.stub.endpoint, api_key=FAKE_API_KEY, timeout_s=5.0),
            model=OPENROUTER_MODEL,
        )

    async def test_stub_content_flows_into_a_prediction_record(self) -> None:
        state = warm_market_states(1)[0]
        self.stub.set_response(StubResponse(body=openrouter_envelope(response_json(confidence=0.7))))
        runtime = PredictionRuntime(provider=self.provider, clock=FakeClock(now=1_000), timeout_ms=5_000, ttl_ms=2_000)

        result = await runtime.submit(state)

        self.assertIs(result.outcome, PredictionOutcome.ACCEPTED)
        record = result.record
        assert record is not None
        self.assertEqual(record.provider, "jev")
        self.assertEqual(record.model, OPENROUTER_MODEL)
        self.assertEqual(record.market_state_hash, market_state_hash(state))
        # raw_response 必须是 Jev content（parser 的输入），不是 OpenRouter 外层
        self.assertEqual(record.raw_response, response_json(confidence=0.7))
        self.assertNotIn("choices", record.raw_response)
        self.assertAlmostEqual(record.prediction.provider_confidence or 0.0, 0.7)

    async def test_recorded_mode_still_avoids_the_network(self) -> None:
        state = warm_market_states(1)[0]
        self.stub.set_response(StubResponse(body=openrouter_envelope(response_json())))
        live = PredictionRuntime(provider=self.provider, clock=FakeClock(), timeout_ms=5_000, ttl_ms=2_000)
        first = await live.submit(state)
        self.assertTrue(first.accepted)

        replay_provider = JevProvider(
            OpenRouterTransport(endpoint=self.stub.endpoint, api_key=FAKE_API_KEY, timeout_s=5.0),
            model=OPENROUTER_MODEL,
        )
        recorded = PredictionRuntime(
            provider=replay_provider,
            clock=FakeClock(),
            timeout_ms=5_000,
            ttl_ms=2_000,
            mode=PredictionMode.RECORDED,
            archive=live.archive,
        )
        before = len(self.stub.requests)
        result = await recorded.submit(state)

        self.assertTrue(result.accepted)
        self.assertEqual(len(self.stub.requests), before, "RECORDED 模式不得发起 HTTP 请求")

    async def test_offline_suite_never_touches_a_remote_host(self) -> None:
        # 本文件所有请求都指向 127.0.0.1 stub（SC-6）
        self.stub.set_response(StubResponse(body=openrouter_envelope(response_json())))
        runtime = PredictionRuntime(provider=self.provider, clock=FakeClock(), timeout_ms=5_000, ttl_ms=2_000)
        await runtime.submit(warm_market_states(1)[0])

        self.assertTrue(self.stub.requests)
        self.assertTrue(self.stub.endpoint.startswith("http://127.0.0.1:"))


class OpenRouterTransportConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        # IsolatedAsyncioTestCase 开启 asyncio debug 模式；本测试故意等待 HTTP 延迟，
        # 因此 asyncio 的 slow-task 日志属于预期噪音（降噪不影响失败判定）。
        logger = logging.getLogger("asyncio")
        previous = logger.level
        logger.setLevel(logging.ERROR)
        self.addCleanup(logger.setLevel, previous)

    async def test_blocking_http_runs_off_the_event_loop(self) -> None:
        with StubOpenRouterServer() as stub:
            stub.set_response(StubResponse(body=openrouter_envelope("{}"), delay_s=0.2))
            transport = OpenRouterTransport(endpoint=stub.endpoint, api_key=FAKE_API_KEY, timeout_s=5.0)
            request = build_prediction_request(warm_market_states(1)[0], sequence=0, created_at=0, ttl_ms=1_000)
            task = asyncio.create_task(transport(request))

            ticks = 0
            while not task.done() and ticks < 10_000:
                await asyncio.sleep(0.001)
                ticks += 1

            self.assertTrue(task.done())
            self.assertGreater(ticks, 5, "HTTP 在飞行中时事件循环应持续可调度")
            self.assertEqual(await task, "{}")
            self.assertEqual(len(stub.requests), 1)


if __name__ == "__main__":
    unittest.main()
