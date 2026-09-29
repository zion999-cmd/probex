"""P0001.10.2 报告确定性（SC-9）：同一组事实 ⇒ 字节级一致的输出。"""

from __future__ import annotations

import unittest

from reports import summary_to_json, summary_to_markdown
from tests.unit.test_report_builder import summary


class ReportDeterminismTest(unittest.TestCase):
    def test_json_is_byte_identical_for_the_same_facts(self) -> None:
        first = summary_to_json(summary())
        second = summary_to_json(summary())
        self.assertEqual(first, second)

    def test_markdown_is_byte_identical_for_the_same_facts(self) -> None:
        self.assertEqual(summary_to_markdown(summary()), summary_to_markdown(summary()))

    def test_fingerprint_order_does_not_affect_output(self) -> None:
        first = summary(config_fingerprints={"b": "2", "a": "1"})
        second = summary(config_fingerprints={"a": "1", "b": "2"})
        self.assertEqual(summary_to_json(first), summary_to_json(second))
        self.assertEqual(summary_to_markdown(first), summary_to_markdown(second))

    def test_changed_fact_changes_output(self) -> None:
        self.assertNotEqual(summary_to_json(summary()), summary_to_json(summary(realized_pnl=-9.0)))
