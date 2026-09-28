"""P0001.4.2 真实集成验证（live test）：默认跳过，需显式 opt-in + OPENROUTER_API_KEY。

```bash
export OPENROUTER_API_KEY=...   # 仅 shell 环境，不写入仓库
export JEV_LIVE_TEST=1
python3 -m unittest -v tests.live.test_systemone_live
```

验证链路：真实 warm MarketState → jev-market-v1 domain payload → SystemOneProvider →
`POST /api/v1/systemone`（model alias `jev-1.13`）→ typed answers → 既有 strict parser → `PredictionRecord`。

设计：**整类共享一次真实批量**（3 个 warm MarketState），所有断言基于该批量证据，避免重复打 provider。
瞬态失败（限流 / 过载 / 超时）允许有限重试；`INVALID_RESPONSE` / `PARSE_ERROR` 属于契约问题，**绝不重试**，
并按提案要求报告差异而非静默修值。不做 prompt engineering、不解析自然语言、不 fallback 到 Chat Completions。
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
import unittest

from prediction.providers.systemone import SystemOneProvider, SystemOneTransport
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import FUTURE_RETURN_HORIZONS_MS, market_state_hash
from prediction.systemone_wire import (
    BINARY_QUESTION_WIRE_IDS,
    SYSTEMONE_API_KEY_ENV,
    SYSTEMONE_MODEL_ALIAS,
    future_return_question_id,
)
from prediction.types import PredictionOutcome, PredictionRecord
from tests.support import warm_market_states

#: 显式 opt-in 开关（避免误触发真实调用与费用）。
LIVE_OPT_IN_ENV = "JEV_LIVE_TEST"

#: 真实调用次数（SC-9 要求「一小组」真实请求 + latency 记录）。
LATENCY_SAMPLES = 3

#: 每个状态允许的尝试次数（仅为瞬态限流/过载保留）。
MAX_ATTEMPTS = 3

#: 测试用 adverse-selection 阈值（bps）。**测试参数，不是生产结论**：
#: 提案未定义该业务阈值，生产值需人类/设计给定。
LIVE_TEST_ADVERSE_SELECTION_THRESHOLD_BPS = 5.0

#: 契约类失败：不重试，必须如实报告。
_HARD_FAILURES = frozenset(
    {PredictionOutcome.INVALID_RESPONSE, PredictionOutcome.PARSE_ERROR, PredictionOutcome.NOT_ELIGIBLE}
)


class _LiveClock:
    """真实单调时钟（毫秒）。

    runtime 用注入时钟计算 `record.latency_ms`；用 FakeClock 会恒为 0，记录里的 latency 就没有证据价值
    （transport telemetry 的 latency_ms 始终是实测值）。
    """

    def now(self) -> int:
        return time.monotonic_ns() // 1_000_000


def _live_enabled() -> bool:
    if os.environ.get(LIVE_OPT_IN_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    return bool(os.environ.get(SYSTEMONE_API_KEY_ENV, "").strip())


@unittest.skipUnless(
    _live_enabled(),
    f"live test 需显式 opt-in（{LIVE_OPT_IN_ENV}=1）且 {SYSTEMONE_API_KEY_ENV} 已设置",
)
class SystemOneLiveTest(unittest.IsolatedAsyncioTestCase):
    transport: SystemOneTransport
    provider: SystemOneProvider
    results: list[object] = []
    records: list[PredictionRecord] = []
    envelopes: list[dict] = []
    attempts: list[int] = []
    states: list = []

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if not _live_enabled():  # pragma: no cover - skipUnless 已挡住
            return

        cls.transport = SystemOneTransport(timeout_s=90.0)
        cls.provider = SystemOneProvider(
            cls.transport,
            adverse_selection_threshold_bps=LIVE_TEST_ADVERSE_SELECTION_THRESHOLD_BPS,
        )
        runtime = PredictionRuntime(
            provider=cls.provider, clock=_LiveClock(), timeout_ms=120_000, ttl_ms=600_000
        )
        cls.states = list(warm_market_states(LATENCY_SAMPLES))
        asyncio.run(cls._collect(runtime))

    @classmethod
    async def _collect(cls, runtime: PredictionRuntime) -> None:
        for state in cls.states:
            result, used = await cls._submit_with_retry(runtime, state)
            cls.results.append(result)
            cls.attempts.append(used)
            if result.record is not None:  # type: ignore[attr-defined]
                cls.records.append(result.record)  # type: ignore[attr-defined]
            cls.envelopes.append(cls._last_envelope())

        cls._report()

    @staticmethod
    async def _submit_with_retry(runtime: PredictionRuntime, state) -> tuple[object, int]:
        result = None
        for index in range(MAX_ATTEMPTS):
            result = await runtime.submit(state)
            if result.outcome is PredictionOutcome.ACCEPTED or result.outcome in _HARD_FAILURES:
                return result, index + 1
            await asyncio.sleep(1.0 * (index + 1))  # 瞬态：限流 / 过载 / 超时
        return result, MAX_ATTEMPTS  # type: ignore[return-value]

    @classmethod
    def _last_envelope(cls) -> dict:
        call = cls.transport.calls[-1]
        try:
            parsed = json.loads(call.response_body)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    @classmethod
    def _report(cls) -> None:
        latencies = [call.latency_ms for call in cls.transport.calls]
        print("\n=== SYSTEM ONE LIVE REPORT ===")
        print(f"endpoint={cls.transport.endpoint} model_alias={SYSTEMONE_MODEL_ALIAS}")
        print(f"adverse_selection_threshold_bps={LIVE_TEST_ADVERSE_SELECTION_THRESHOLD_BPS} (测试参数)")
        print(f"attempts_per_state={cls.attempts}")
        print(
            f"transport_latency_ms={latencies} min={min(latencies)} "
            f"median={int(statistics.median(latencies))} max={max(latencies)}"
        )
        for index, envelope in enumerate(cls.envelopes):
            answers = envelope.get("answers") or {}
            print(
                f"  sample {index}: id={envelope.get('id')!r} model={envelope.get('model')!r} "
                f"provider={envelope.get('provider')!r} usage={envelope.get('usage')}"
            )
            for question_id, answer in sorted(answers.items()):
                if not isinstance(answer, dict):
                    continue
                detail = {key: answer.get(key) for key in ("type", "choice", "confidence") if key in answer}
                if answer.get("type") == "noul":
                    detail["noul"] = answer.get("noul")
                print(f"    {question_id}: {detail}")

    def _accepted(self) -> list[PredictionRecord]:
        if len(self.records) != LATENCY_SAMPLES:
            diagnostics = [
                {
                    "outcome": result.outcome.value,  # type: ignore[attr-defined]
                    "error": repr(result.error),  # type: ignore[attr-defined]
                    "raw": envelope,
                }
                for result, envelope in zip(self.results, self.envelopes)
            ]
            self.fail(f"CONTRACT_MISMATCH / PROVIDER_UNSUITABLE: 未全部获得记录\n{json.dumps(diagnostics, ensure_ascii=False, indent=2)[:3000]}")
        return self.records

    # ------------------------------------------------------------------ SCs

    async def test_sc1_real_chain_produces_prediction_records(self) -> None:
        records = self._accepted()

        self.assertEqual(len(records), LATENCY_SAMPLES)
        for record, state in zip(records, self.states):
            with self.subTest(request_id=record.request_id):
                self.assertEqual(record.market_state_hash, market_state_hash(state))
                self.assertEqual(len(record.prediction.future_return), 4)

    async def test_sc2_every_horizon_has_a_complete_five_class_distribution(self) -> None:
        for record in self._accepted():
            distributions = {d.horizon_ms: d for d in record.prediction.future_return}
            self.assertEqual(sorted(distributions), sorted(FUTURE_RETURN_HORIZONS_MS))
            for horizon, distribution in distributions.items():
                with self.subTest(horizon=horizon):
                    values = dict(distribution.as_mapping())
                    self.assertEqual(len(values), 5)
                    self.assertTrue(all(0.0 <= value <= 1.0 for value in values.values()))
                    self.assertAlmostEqual(distribution.probability_sum, 1.0, places=6)

    async def test_sc3_binary_questions_are_probabilities(self) -> None:
        for record in self._accepted():
            for value in (
                record.prediction.buy_adverse_selection,
                record.prediction.sell_adverse_selection,
                record.prediction.buy_fill_probability,
                record.prediction.sell_fill_probability,
            ):
                with self.subTest(value=value):
                    self.assertGreaterEqual(value, 0.0)
                    self.assertLessEqual(value, 1.0)

    async def test_sc4_noul_answers_carry_no_confidence(self) -> None:
        self._accepted()

        for envelope in self.envelopes:
            answers = envelope.get("answers") or {}
            for domain_question, wire_id in BINARY_QUESTION_WIRE_IDS.items():
                with self.subTest(question=domain_question):
                    answer = answers[wire_id]
                    self.assertEqual(answer["type"], "noul")
                    self.assertNotIn("confidence", answer, "Noul 协议上没有 confidence")
            nearest = answers[future_return_question_id(min(FUTURE_RETURN_HORIZONS_MS))]
            self.assertEqual(nearest["type"], "choice")
            self.assertIn("confidence", nearest, "Choice 才带 confidence")

    async def test_sc5_records_carry_provider_evidence(self) -> None:
        for record in self._accepted():
            with self.subTest(request_id=record.request_id):
                self.assertEqual(record.requested_model, SYSTEMONE_MODEL_ALIAS)
                self.assertEqual(record.model, SYSTEMONE_MODEL_ALIAS)
                self.assertTrue(record.resolved_model and "jev" in record.resolved_model)
                self.assertEqual(record.provider, "TypeSafe")
                self.assertTrue(record.response_id and record.response_id.startswith("gen-dec-"))
                assert record.usage is not None
                self.assertIsNotNone(record.usage.input_tokens)
                self.assertIsNotNone(record.usage.cost)
                self.assertGreater(record.latency_ms, 0)
                self.assertGreater(record.prediction.derived_confidence, 0.0)

    async def test_sc6_unknown_fields_do_not_reach_the_prediction(self) -> None:
        for record in self._accepted():
            domain = json.loads(record.raw_response)
            with self.subTest(request_id=record.request_id):
                self.assertEqual(
                    sorted(domain),
                    [
                        "buy_adverse_selection",
                        "buy_fill_probability",
                        "confidence",
                        "future_return",
                        "question_schema_version",
                        "sell_adverse_selection",
                        "sell_fill_probability",
                    ],
                )
                for leaked in ("reasoning", "caveat", "assessment", "advice", "explanation", "limitations"):
                    self.assertNotIn(leaked, record.raw_response)

    async def test_sc9_latency_is_measured_over_a_small_real_group(self) -> None:
        self._accepted()

        transport_latencies = [call.latency_ms for call in self.transport.calls]
        record_latencies = [record.latency_ms for record in self.records]

        self.assertEqual(len(transport_latencies), LATENCY_SAMPLES)
        self.assertTrue(all(value > 0 for value in transport_latencies))
        self.assertTrue(all(value > 0 for value in record_latencies))
        print(
            "\n=== SYSTEM ONE LATENCY ===\n"
            f"transport_ms={transport_latencies} record_ms={record_latencies}\n"
            f"attempts={self.attempts}\n"
        )

    async def test_sc7_credentials_are_not_persisted(self) -> None:
        key = os.environ.get(SYSTEMONE_API_KEY_ENV, "")
        self.assertTrue(key)
        self.assertTrue(self.transport.has_api_key)

        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for root, _dirs, files in os.walk(repo_root):
            if ".git" in root:
                continue
            for name in files:
                if not name.endswith((".py", ".json", ".md", ".txt")):
                    continue
                with open(os.path.join(root, name), encoding="utf-8", errors="ignore") as handle:
                    self.assertNotIn(key, handle.read(), f"Key 出现在 {name} 中")


if __name__ == "__main__":
    unittest.main()
