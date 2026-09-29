"""P0001.11 §4 / 裁决 D：统一 blocker 投影（不排序、不丢弃、去重键固定）。"""

from __future__ import annotations

import unittest

from product.blockers import (
    BlockerOwner,
    BlockerSeverity,
    BlockerView,
    blocking_count,
    dedupe_blockers,
    project_blockers,
)
from tests.unit.test_product_snapshot import service


class BlockerProjectionTest(unittest.TestCase):
    def test_readiness_risk_strategy_and_orchestrator_are_all_projected(self) -> None:
        snapshot = service().snapshot()
        blockers = project_blockers(snapshot=snapshot,
                                    orchestrator_notes=("reconciling:awaiting_pairs", "authority_invalid:NONE"))

        owners = {blocker.owner for blocker in blockers}
        self.assertIn(BlockerOwner.READINESS, owners)
        self.assertIn(BlockerOwner.RISK, owners)
        self.assertIn(BlockerOwner.ORCHESTRATOR, owners)
        readiness = [b for b in blockers if b.owner is BlockerOwner.READINESS]
        self.assertEqual(readiness[0].reason_code, "PRIVATE_LATENCY_UNOBSERVED")
        self.assertEqual(readiness[0].severity, BlockerSeverity.BLOCKING)
        self.assertEqual(readiness[0].source_ref, "readiness.reasons[0]")
        orchestrator = [b for b in blockers if b.owner is BlockerOwner.ORCHESTRATOR]
        self.assertEqual([b.reason_code for b in orchestrator], ["ORCHESTRATOR_RECONCILING", "AUTHORITY_INVALID"])

    def test_unknown_readiness_is_a_blocking_unknown_not_a_silent_pass(self) -> None:
        snapshot = service(readiness=lambda: None).snapshot()
        blockers = project_blockers(snapshot=snapshot)

        readiness = [b for b in blockers if b.owner is BlockerOwner.READINESS]
        self.assertEqual(len(readiness), 1)
        self.assertEqual(readiness[0].reason_code, "READINESS_UNKNOWN")
        self.assertEqual(readiness[0].severity, BlockerSeverity.BLOCKING)

    def test_market_and_prediction_facts_produce_blockers_only_when_known(self) -> None:
        healthy = project_blockers(snapshot=service().snapshot())
        self.assertFalse([b for b in healthy if b.owner is BlockerOwner.MARKET])
        unhealthy = project_blockers(snapshot=service(
            market_state=lambda: __import__("types").SimpleNamespace(
                identity=__import__("types").SimpleNamespace(state_hash="m"),
                quality=__import__("types").SimpleNamespace(healthy=False, tradeable=False, window_coverage_ms=1),
                price=__import__("types").SimpleNamespace(best_bid=1.0, best_ask=2.0, spread_bps=1.0),
            )).snapshot())
        reasons = {b.reason_code for b in unhealthy if b.owner is BlockerOwner.MARKET}
        self.assertEqual(reasons, {"MARKET_UNHEALTHY", "MARKET_NOT_TRADEABLE"})

    def test_unknown_market_facts_do_not_fabricate_blockers(self) -> None:
        snapshot = service(market_state=lambda: None).snapshot()
        blockers = project_blockers(snapshot=snapshot)

        self.assertFalse([b for b in blockers if b.owner is BlockerOwner.MARKET])

    def test_low_severity_notes_are_kept_not_dropped(self) -> None:
        """裁决 D：不得丢弃低优先级 blocker（UI 可排序，但投影不丢）。"""
        blockers = project_blockers(snapshot=None, orchestrator_notes=("something_happened:detail",))

        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].severity, BlockerSeverity.INFO)
        self.assertEqual(blockers[0].reason_code, "SOMETHING_HAPPENED")
        self.assertEqual(blockers[0].message, "detail")

    def test_dedupe_key_is_owner_reason_source(self) -> None:
        blocker = BlockerView(owner=BlockerOwner.RISK, reason_code="MAX_POSITION_EXCEEDED",
                              severity=BlockerSeverity.BLOCKING, message="m", source_ref="risk.rejects[0]")
        duplicate = BlockerView(owner=BlockerOwner.RISK, reason_code="MAX_POSITION_EXCEEDED",
                                severity=BlockerSeverity.BLOCKING, message="other", source_ref="risk.rejects[0]")
        different_source = BlockerView(owner=BlockerOwner.RISK, reason_code="MAX_POSITION_EXCEEDED",
                                       severity=BlockerSeverity.BLOCKING, message="m",
                                       source_ref="risk.rejects[1]")

        self.assertEqual(blocker.key, duplicate.key)
        self.assertEqual(dedupe_blockers([blocker, duplicate]), (blocker,))
        self.assertEqual(len(dedupe_blockers([blocker, different_source])), 2)

    def test_blocking_count_counts_only_blocking(self) -> None:
        blockers = project_blockers(snapshot=service().snapshot(), orchestrator_notes=("noise:x",))
        self.assertEqual(blocking_count(blockers), len([b for b in blockers
                                                        if b.severity is BlockerSeverity.BLOCKING]))

    def test_blocker_fields_are_mandatory(self) -> None:
        with self.assertRaises(ValueError):
            BlockerView(owner=BlockerOwner.RISK, reason_code="", severity=BlockerSeverity.BLOCKING,
                        message="m", source_ref="s")
        with self.assertRaises(TypeError):
            BlockerView(owner="RISK", reason_code="x", severity=BlockerSeverity.BLOCKING, message="m",
                        source_ref="s")  # type: ignore[arg-type]
