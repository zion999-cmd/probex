"""P0001.9.4.1.1 单元测试：public market 事实 → `market_ready` 的 harness 映射。

SC-2：market readiness **不得**由硬编码 bool 假装成立 —— 这里用真实字段的合成事实验证映射逻辑
（真实 public 数据在 `tests/live/test_binance_full_readiness_live.py` 的 opt-in 运行中采集）。
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from market.readiness import (
    MarketReadinessError,
    MarketReadinessEvidence,
    MarketReadinessPolicy,
    build_market_evidence,
    market_ready,
)
from tests.live.market_readiness import market_evidence_report


def policy(**overrides: object) -> MarketReadinessPolicy:
    values: dict[str, object] = {"max_mark_age_ms": 5_000, "max_feed_age_ms": 5_000}
    values.update(overrides)
    return MarketReadinessPolicy(**values)  # type: ignore[arg-type]


def healthy(**overrides: object) -> MarketReadinessEvidence:
    values: dict[str, object] = {
        "ready": True,
        "observed_at": 1_000,
        "generation": 3,
        "book_health": "healthy",
        "anchored": True,
        "mark_age_ms": 250,
        "feed_age_ms": 120,
        "depth_gap_count": 0,
        "resync_count": 0,
        "malformed_count": 0,
        "agg_trade_count": 120,
        "problems": (),
    }
    values.update(overrides)
    return MarketReadinessEvidence(**values)  # type: ignore[arg-type]


class MarketReadyMappingTest(unittest.TestCase):
    def test_all_facts_present_is_ready(self) -> None:
        ready, problems = market_ready(healthy(), policy=policy())

        self.assertTrue(ready)
        self.assertEqual(problems, ())

    def test_every_missing_fact_blocks(self) -> None:
        cases = {
            "book": {"book_health": "stale"},
            "awaiting": {"book_health": "awaiting_snapshot"},
            "resyncing": {"book_health": "resyncing"},
            "anchor": {"anchored": False},
            "resync": {"resync_count": 2},
            "gap": {"depth_gap_count": 1},
            "malformed": {"malformed_count": 3},
            "no_trades": {"agg_trade_count": 0},
            "mark_missing": {"mark_age_ms": None},
            "mark_stale": {"mark_age_ms": 5_001},
            "feed_missing": {"feed_age_ms": None},
            "feed_stale": {"feed_age_ms": 9_999},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                ready, problems = market_ready(healthy(**overrides), policy=policy())
                self.assertFalse(ready)
                self.assertTrue(problems)

    def test_boundary_values_are_inclusive(self) -> None:
        ready, problems = market_ready(
            healthy(mark_age_ms=5_000, feed_age_ms=5_000), policy=policy()
        )

        self.assertTrue(ready, problems)

    def test_policy_thresholds_must_be_explicit(self) -> None:
        with self.assertRaises(TypeError):
            MarketReadinessPolicy()  # type: ignore[call-arg]
        for bad in ({"max_mark_age_ms": 0}, {"max_feed_age_ms": -1}, {"max_mark_age_ms": True}):
            with self.subTest(bad=bad):
                with self.assertRaises(MarketReadinessError):
                    policy(**bad)

    def test_report_lists_all_problems(self) -> None:
        evidence = build_market_evidence(
            observed_at=1_000,
            generation=3,
            book_health="stale",
            anchored=True,
            mark_age_ms=250,
            feed_age_ms=120,
            depth_gap_count=1,
            resync_count=0,
            malformed_count=0,
            agg_trade_count=0,
            policy=policy(),
        )

        report = market_evidence_report(evidence)

        self.assertFalse(report["ready"])
        self.assertEqual(len(report["problems"]), 3)
        self.assertEqual(report["book_health"], "stale")
        self.assertEqual(report["depth_gap_count"], 1)

    def test_builder_keeps_ready_and_problems_consistent(self) -> None:
        evidence = build_market_evidence(
            observed_at=1_000, generation=1, book_health="healthy", anchored=True,
            mark_age_ms=1, feed_age_ms=1, depth_gap_count=0, resync_count=0, malformed_count=0,
            agg_trade_count=5, policy=policy(),
        )

        self.assertTrue(evidence.ready)
        self.assertEqual(evidence.problems, ())

    def test_replacement_can_build_new_facts(self) -> None:
        """事实对象是 frozen dataclass：只能构造新的，不能就地修改。"""
        evidence = healthy()

        changed = replace(evidence, book_health="stale")

        self.assertEqual(evidence.book_health, "healthy")
        self.assertEqual(changed.book_health, "stale")
        with self.assertRaises(Exception):
            setattr(evidence, "book_health", "stale")


if __name__ == "__main__":
    unittest.main()
