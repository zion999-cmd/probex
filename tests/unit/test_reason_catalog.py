"""F-09 Reason Code 人类解释层（catalog 是 presentation-only，不参与决策）。"""

from __future__ import annotations

import pathlib
import unittest

from product import reason_catalog
from product.reason_catalog import (CATALOG, catalog_payload, explain_codes, explain_reason,
                                    lookup)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class CatalogCoverageTest(unittest.TestCase):
    def test_required_domains_are_covered(self) -> None:
        domains = {entry.domain for entry in CATALOG.values()}
        for required in ("market", "prediction", "readiness", "hwm", "execution", "rate_limit",
                         "reconciliation", "accounting"):
            with self.subTest(domain=required):
                self.assertIn(required, domains)

    def test_displayed_codes_have_entries(self) -> None:
        displayed = ("MARKET_UNHEALTHY", "MARKET_NOT_TRADEABLE", "PREDICTION_STALE",
                     "RECOVERY_NOT_READY", "HISTORICAL_DRAWDOWN_UNKNOWN",
                     "HIGH_WATERMARK_NOT_INITIALIZED", "EXECUTION_POLICY_NOT_CONFIGURED",
                     "RATE_LIMIT_EXHAUSTED", "RECONCILIATION_REQUIRED", "ACCOUNTING_UNKNOWN",
                     "PREDICTION_PROVIDER_UNKNOWN", "PRIVATE_LATENCY_UNOBSERVED")
        for code in displayed:
            with self.subTest(code=code):
                entry = lookup(code)
                self.assertIsNotNone(entry, code)
                self.assertTrue(entry.title and entry.explanation and entry.suggested_next_step)
                self.assertTrue(entry.title_en and entry.explanation_en)

    def test_every_entry_has_the_required_structure(self) -> None:
        payload = catalog_payload()
        self.assertEqual(len(payload), len(CATALOG))
        for item in payload:
            for key in ("reason_code", "title", "explanation", "severity", "suggested_next_step",
                        "domain", "catalogued"):
                self.assertIn(key, item)
            self.assertTrue(item["catalogued"])


class UnknownCodeTest(unittest.TestCase):
    def test_unknown_code_keeps_raw_code_and_does_not_guess(self) -> None:
        entry = explain_reason("FUTURE_CODE_XYZ")
        self.assertEqual(entry.reason_code, "FUTURE_CODE_XYZ")
        self.assertEqual(entry.title, reason_catalog.UNKNOWN_TITLE)
        self.assertEqual(entry.severity, "UNKNOWN")
        self.assertEqual(entry.domain, "unknown")
        self.assertFalse(entry.catalogued)
        self.assertNotIn("FUTURE_CODE_XYZ", CATALOG)

    def test_non_string_code_is_stringified_not_invented(self) -> None:
        self.assertEqual(explain_reason(42).reason_code, "42")
        self.assertFalse(explain_reason(42).catalogued)

    def test_explain_codes_is_sorted_deduped_and_includes_unknown(self) -> None:
        mapping = explain_codes(["PREDICTION_STALE", "PREDICTION_STALE", "NOPE"])
        self.assertEqual(list(mapping), ["NOPE", "PREDICTION_STALE"])
        self.assertFalse(mapping["NOPE"]["catalogued"])


class PurityTest(unittest.TestCase):
    def test_catalog_does_not_import_decision_modules(self) -> None:
        source = (PROJECT_ROOT / "product" / "reason_catalog.py").read_text(encoding="utf-8")
        for forbidden in ("import risk", "import readiness", "import execution_safety",
                          "from risk", "from readiness", "from execution_safety"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
