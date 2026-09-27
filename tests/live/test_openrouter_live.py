"""真实调用验证（live test）：默认跳过，只有显式 opt-in + OPENROUTER_API_KEY 才执行。

运行方式（凭证只在 shell 环境里，不写入仓库、不进入日志）：

```bash
export OPENROUTER_API_KEY=...      # 仅本机 shell
export JEV_LIVE_TEST=1             # 显式 opt-in（避免误产生真实调用）
python3 -m unittest -v tests.live.test_openrouter_live
```

本文件只做验证与报告，不做任何业务转换：

1. 用已 warm-up 的真实 `MarketState` 构造 `jev-market-v1` 请求；
2. 经 `OpenRouterTransport` → `typesafe/jev-router` 取回 `choices[0].message.content`；
3. 打印 `CONTRACT REPORT`（envelope / content 结构 / 旧 FMZ 语义 / 五分类求和 / latency）；
4. 把 content 交给既有 `JevProvider` + strict parser，验证能否产出 `PredictionRecord`；
5. 若与 `jev-market-v1` 不一致：报告 `CONTRACT_MISMATCH` 及具体差异并判失败，
   绝不静默修值、也不改动 Prediction Runtime 上层。
"""

from __future__ import annotations

import json
import os
import statistics
import unittest

from prediction.providers.jev import JevProvider
from prediction.providers.openrouter import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_ENDPOINT,
    OPENROUTER_MODEL,
    OpenRouterTransport,
)
from prediction.runtime import PredictionRuntime
from prediction.schema.market_v1 import market_state_hash
from prediction.types import PredictionOutcome
from tests.fakes import FakeClock
from tests.live.contract_report import ContractReport, describe_content, describe_envelope
from tests.support import warm_market_states

#: 显式 opt-in 开关，避免误触发真实调用（会产生真实请求与费用）。
LIVE_OPT_IN_ENV = "JEV_LIVE_TEST"

#: 真实调用次数（用于 latency 分布；提案要求「实测」而非假设）。
LATENCY_SAMPLES = 3


def _live_enabled() -> bool:
    if os.environ.get(LIVE_OPT_IN_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    return bool(os.environ.get(OPENROUTER_API_KEY_ENV, "").strip())


@unittest.skipUnless(
    _live_enabled(),
    f"live test 需显式 opt-in（{LIVE_OPT_IN_ENV}=1）且 {OPENROUTER_API_KEY_ENV} 已设置",
)
class OpenRouterLiveContractTest(unittest.IsolatedAsyncioTestCase):
    """真实 OpenRouter × typesafe/jev-router 的契约验证。"""

    def setUp(self) -> None:
        super().setUp()
        # 只用环境变量提供凭证；Key 不进入任何断言输出。
        self.transport = OpenRouterTransport(timeout_s=60.0)
        self.states = warm_market_states(LATENCY_SAMPLES)

    def _report(self, report: ContractReport) -> None:
        print("\n" + report.render())

    async def test_live_chain_produces_a_prediction_record(self) -> None:
        """SC-1 / SC-2 / SC-5：真实往返 + 契约一致性 + 实测 latency。"""
        from prediction.schema.market_v1 import build_prediction_request

        state = self.states[0]
        request = build_prediction_request(state, sequence=0, created_at=0, ttl_ms=60_000)

        content = await self.transport(request)
        call = self.transport.calls[-1]
        envelope_ok, envelope_keys, extracted = describe_envelope(call.response_body)
        report = describe_content(
            extracted or content,
            latency_ms=call.latency_ms,
            envelope_ok=envelope_ok,
            envelope_keys=envelope_keys,
        )
        self._report(report)

        self.assertTrue(envelope_ok, "实际响应必须符合 OpenRouter envelope（choices[0].message.content）")
        self.assertEqual(extracted, content)
        self.assertGreater(call.latency_ms, 0, "latency 必须是实测值")

        provider = JevProvider(self.transport, model=OPENROUTER_MODEL)
        runtime = PredictionRuntime(provider=provider, clock=FakeClock(now=0), timeout_ms=90_000, ttl_ms=60_000)
        result = await runtime.submit(state)

        if result.outcome is not PredictionOutcome.ACCEPTED:
            self.fail(
                "CONTRACT_MISMATCH: 真实 Jev content 无法进入现有 strict parser。\n"
                f"outcome={result.outcome.value}\n"
                f"error={result.error!r}\n"
                f"mismatches={list(report.mismatches)}\n"
                "处理方式（不得静默修值）：在 OpenRouter adapter 做结构转换，或升级 question_schema_version。\n"
                f"--- 原始 content ---\n{content}"
            )

        record = result.record
        assert record is not None
        self.assertEqual(record.model, OPENROUTER_MODEL)
        self.assertEqual(record.market_state_hash, market_state_hash(state))
        self.assertEqual(record.question_schema_version, "jev-market-v1")
        self.assertIn(record.provider, {"jev", "openrouter"})
        self.assertTrue(record.raw_response)
        self.assertTrue(report.matches_jev_market_v1, f"contract mismatches: {list(report.mismatches)}")

    async def test_live_latency_distribution_is_measured(self) -> None:
        """SC-5：记录实测 latency 分布数量级，不做假设。"""
        from prediction.schema.market_v1 import build_prediction_request

        for index, state in enumerate(self.states):
            request = build_prediction_request(state, sequence=index, created_at=0, ttl_ms=60_000)
            await self.transport(request)

        latencies = [call.latency_ms for call in self.transport.calls]
        print(
            "\n=== JEV LIVE LATENCY ===\n"
            f"endpoint: {OPENROUTER_ENDPOINT}\n"
            f"model: {OPENROUTER_MODEL}\n"
            f"samples_ms: {latencies}\n"
            f"min_ms: {min(latencies)} median_ms: {int(statistics.median(latencies))} max_ms: {max(latencies)}\n"
        )

        self.assertEqual(len(latencies), LATENCY_SAMPLES)
        self.assertTrue(all(value > 0 for value in latencies))
        self.assertTrue(all(call.error is None for call in self.transport.calls))

    async def test_live_content_structure_report(self) -> None:
        """SC-2 证据：输出真实 content 的结构诊断（market_* / toxicity_* / fill_* 等）。"""
        from prediction.schema.market_v1 import build_prediction_request

        request = build_prediction_request(self.states[0], sequence=0, created_at=0, ttl_ms=60_000)
        await self.transport(request)
        call = self.transport.calls[-1]

        envelope_ok, envelope_keys, content = describe_envelope(call.response_body)
        report = describe_content(content, latency_ms=call.latency_ms, envelope_ok=envelope_ok, envelope_keys=envelope_keys)
        self._report(report)

        self.assertTrue(envelope_ok)
        self.assertTrue(report.content_is_json_object, "Jev content 必须是 JSON object")
        self.assertTrue(report.content_keys, "必须能报告真实 content 的顶层键")
        # 诊断报告本身必须可解释：若与 jev-market-v1 不一致，mismatches 必须非空
        if not report.matches_jev_market_v1:
            print("\nCONTRACT_MISMATCH (诊断):\n" + json.dumps(list(report.mismatches), ensure_ascii=False, indent=2))

    def test_live_credentials_are_not_persisted(self) -> None:
        """SC-4：Key 只存在于环境变量，不进入仓库。"""
        key = os.environ.get(OPENROUTER_API_KEY_ENV, "")
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
