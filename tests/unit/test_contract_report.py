"""契约诊断器（live test 的报告器）自身的行为验证。

live test 默认跳过，因此这里用离线样本确保诊断逻辑可用：
- 符合 `jev-market-v1` 的 content → 无 mismatch；
- 旧 FMZ V3 风格 content（market_* / toxicity_* / fill_*）→ 明确报出差异与缺失结构。
"""

from __future__ import annotations

import json
import unittest

from tests.fakes import response_json, response_payload
from tests.live.contract_report import (
    LEGACY_FAMILIES,
    describe_content,
    describe_envelope,
)
from tests.stub_server import openrouter_envelope


class DescribeEnvelopeTest(unittest.TestCase):
    def test_valid_envelope(self) -> None:
        ok, keys, content = describe_envelope(openrouter_envelope('{"a":1}'))
        self.assertTrue(ok)
        self.assertIn("choices", keys)
        self.assertIn("model", keys)
        self.assertEqual(content, '{"a":1}')

    def test_invalid_envelopes(self) -> None:
        for body in ("", "not json", "[]", '{"choices":[]}', '{"choices":[{"message":{}}]}'):
            with self.subTest(body=body):
                ok, _keys, content = describe_envelope(body)
                self.assertFalse(ok)
                self.assertEqual(content, "")


class DescribeContentTest(unittest.TestCase):
    def test_conformant_content_has_no_mismatches(self) -> None:
        report = describe_content(response_json(confidence=0.6), latency_ms=123)

        self.assertTrue(report.content_is_json_object)
        self.assertTrue(report.matches_jev_market_v1, f"unexpected mismatches: {report.mismatches}")
        self.assertEqual(report.future_return_horizons, ("15s", "30s", "5s", "60s"))
        self.assertEqual(set(report.probability_sums), {"5s", "15s", "30s", "60s"})
        for total in report.probability_sums.values():
            self.assertAlmostEqual(total, 1.0)
        self.assertEqual(report.latency_ms, 123)
        self.assertTrue(report.confidence_present)
        self.assertTrue(all(report.probability_question_presence.values()))

    def test_legacy_fmz_content_reports_structural_differences(self) -> None:
        legacy = json.dumps(
            {
                "market_5s": {"choice": "flat", "probabilities": {"flat": 0.6}, "confidence": 0.8},
                "toxicity_5s": {"score": 0.2, "probabilities": {"low": 0.7, "high": 0.3}},
                "fill_buy": {"noul": 0.4, "probability": 0.3, "value": 0.9},
            }
        )
        report = describe_content(legacy)

        self.assertFalse(report.matches_jev_market_v1)
        self.assertTrue(report.mismatches)
        self.assertIn("market_", report.legacy_families)
        self.assertIn("market_5s", report.legacy_families["market_"])
        summary = report.legacy_families["market_"]["market_5s"]
        self.assertIn("choice", summary)
        self.assertIn("noul", report.legacy_families["fill_"]["fill_buy"])

    def test_non_json_content_is_reported(self) -> None:
        report = describe_content("no json here")
        self.assertFalse(report.content_is_json_object)
        self.assertTrue(any("not valid JSON" in item for item in report.mismatches))

    def test_non_object_content_is_reported(self) -> None:
        report = describe_content("[1,2,3]")
        self.assertTrue(any("JSON object" in item for item in report.mismatches))

    def test_missing_category_is_reported(self) -> None:
        payload = response_payload()
        del payload["future_return"]["5s"]["flat"]  # type: ignore[index]
        report = describe_content(json.dumps(payload))

        self.assertFalse(report.matches_jev_market_v1)
        self.assertIn("5s", report.future_return_missing_categories)
        self.assertIn("flat", report.future_return_missing_categories["5s"])

    def test_wrong_probability_sum_is_reported(self) -> None:
        payload = response_payload()
        payload["future_return"]["5s"]["flat"] = 0.95  # type: ignore[index]
        report = describe_content(json.dumps(payload))

        self.assertFalse(report.matches_jev_market_v1)
        self.assertGreater(report.probability_sums["5s"], 1.0)
        self.assertTrue(any("probabilities sum" in item for item in report.mismatches))

    def test_missing_probability_question_is_reported(self) -> None:
        payload = response_payload()
        del payload["buy_fill_probability"]
        report = describe_content(json.dumps(payload))

        self.assertFalse(report.matches_jev_market_v1)
        self.assertFalse(report.probability_question_presence["buy_fill_probability"])

    def test_unexpected_horizon_is_reported(self) -> None:
        payload = response_payload()
        payload["future_return"]["120s"] = dict(payload["future_return"]["5s"])  # type: ignore[index]
        report = describe_content(json.dumps(payload))

        self.assertFalse(report.matches_jev_market_v1)
        self.assertTrue(any("unexpected horizons" in item for item in report.mismatches))

    def test_report_render_contains_key_evidence(self) -> None:
        rendered = describe_content(response_json(), latency_ms=42).render()
        for needle in ("JEV CONTRACT REPORT", "envelope_ok", "content_keys", "latency_ms: 42", "matches_jev_market_v1"):
            with self.subTest(needle=needle):
                self.assertIn(needle, rendered)

    def test_legacy_families_cover_documented_semantics(self) -> None:
        self.assertEqual(set(LEGACY_FAMILIES), {"market_", "toxicity_", "fill_"})
        self.assertIn("choice", LEGACY_FAMILIES["market_"])
        self.assertIn("noul", LEGACY_FAMILIES["fill_"])


if __name__ == "__main__":
    unittest.main()
