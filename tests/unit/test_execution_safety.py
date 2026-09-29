"""P0001.13 单测：policy（无默认值）/ governor / latency / health / projection blockers。"""

from __future__ import annotations

import pathlib
import unittest

from execution_safety import (
    BoundedLatencyLog,
    ExecutionSafetyPolicy,
    ExecutionSafetyPolicyError,
    ExecutionSafetyProjection,
    HealthRule,
    LATENCY_STAGES,
    LatencySample,
    RateLimitStatus,
    Severity,
    evaluate_governor,
    evaluate_health,
    evaluate_latency,
)
from execution_safety.types import ExecutionHealthStatus, HEALTH_SOURCES
from execution_safety.venue import VenueRateLimitFacts, venue_limit_state
from connectors.binance.market_data.exchange_info import TradingRules
from product.types import BlockerOwner, BlockerSeverity

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = 1_000_000


def rules() -> TradingRules:
    return TradingRules(symbol="BTCUSDT", status="TRADING", tick_size=0.1, min_price=100.0,
                        max_price=1_000_000.0, step_size=0.0001, min_qty=0.0001, max_qty=100.0,
                        min_notional=50.0)


def health_rules(**overrides: object) -> dict[str, HealthRule]:
    base = {source: HealthRule(severity=Severity.BLOCKING, downgrades=True, required=True)
            for source in HEALTH_SOURCES}
    base.update(overrides)
    return base


def policy(**overrides: object) -> ExecutionSafetyPolicy:
    values: dict[str, object] = {
        "near_limit_ratio": 0.2, "exhausted_ratio": 0.05, "order_rate_near_limit_ratio": 0.25,
        "venue_facts_max_age_ms": 60_000,
        "latency_budget_ms": {stage: 500 for stage in LATENCY_STAGES},
        "latency_min_samples": 5, "latency_window_ms": 600_000,
        "reconciliation_required_after_unknown": True, "health_rules": health_rules(),
    }
    values.update(overrides)
    return ExecutionSafetyPolicy(**values)  # type: ignore[arg-type]


def rate_facts(*, used_weight: int | None, limit: int | None, used_orders: int | None = 0,
               order_limit: int | None = 100) -> VenueRateLimitFacts:
    return VenueRateLimitFacts(request_weight_limit=limit, order_rate_limit=order_limit, window_ms=60_000,
                               used_weight=used_weight, used_orders=used_orders, reset_at_ms=NOW + 30_000,
                               source="test:evidence-confirmed", observed_at_ms=NOW)


class PolicyTest(unittest.TestCase):
    def test_1_policy_requires_every_field_without_defaults(self) -> None:
        with self.assertRaises(TypeError):
            ExecutionSafetyPolicy()  # type: ignore[call-arg]

    def test_policy_rejects_missing_latency_stages_or_health_rules(self) -> None:
        with self.assertRaises(ExecutionSafetyPolicyError):
            policy(latency_budget_ms={"submit_to_ack": 100})
        with self.assertRaises(ExecutionSafetyPolicyError):
            policy(health_rules={"rate_limit_state": HealthRule(Severity.BLOCKING, True, True)})

    def test_policy_rejects_inconsistent_ratios(self) -> None:
        with self.assertRaises(ExecutionSafetyPolicyError):
            policy(near_limit_ratio=0.05, exhausted_ratio=0.2)
        with self.assertRaises(ExecutionSafetyPolicyError):
            policy(near_limit_ratio=1.5)

    def test_required_sources_are_exposed(self) -> None:
        self.assertEqual(set(policy().required_sources), set(HEALTH_SOURCES))


class GovernorTest(unittest.TestCase):
    def test_2_missing_venue_evidence_stays_unknown(self) -> None:
        state = evaluate_governor(policy=policy(), facts=None, now_ms=NOW)

        self.assertIs(state.request.status, RateLimitStatus.UNKNOWN)
        self.assertIs(state.order.status, RateLimitStatus.UNKNOWN)
        self.assertFalse(state.request.limit.known)
        self.assertIsNone(state.request.limit.value)
        # UNKNOWN 不阻止新增暴露（没有证据就不假装耗尽），也不阻止降险
        self.assertTrue(state.allows_new_exposure)
        self.assertTrue(state.allows_de_risking)

    def test_5_near_limit_and_exhausted_are_testable(self) -> None:
        near = evaluate_governor(policy=policy(), facts=rate_facts(used_weight=850, limit=1_000), now_ms=NOW)
        exhausted = evaluate_governor(policy=policy(), facts=rate_facts(used_weight=990, limit=1_000), now_ms=NOW)

        self.assertIs(near.request.status, RateLimitStatus.NEAR_LIMIT)
        self.assertTrue(near.allows_new_exposure)
        self.assertIs(exhausted.request.status, RateLimitStatus.EXHAUSTED)
        self.assertFalse(exhausted.allows_new_exposure)
        self.assertTrue(exhausted.allows_de_risking)   # SC-19

    def test_order_budget_uses_its_own_ratio(self) -> None:
        state = evaluate_governor(policy=policy(), facts=rate_facts(used_weight=0, limit=1_000,
                                                                   used_orders=80, order_limit=100),
                                  now_ms=NOW)
        self.assertIs(state.order.status, RateLimitStatus.NEAR_LIMIT)

    def test_4_governor_has_no_retry_logic(self) -> None:
        source = (PROJECT_ROOT / "execution_safety" / "rate_limit.py").read_text(encoding="utf-8")
        for forbidden in ("retry(", "resubmit(", "submit(", "sleep(", "while True"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source)


class LatencyTest(unittest.TestCase):
    def test_7_insufficient_samples_stay_unknown_not_zero(self) -> None:
        log = BoundedLatencyLog(capacity=50)
        log.record(LatencySample(stage="submit_to_ack", ts=NOW, value_ms=120))

        state = evaluate_latency(policy=policy(latency_min_samples=5), log=log, now_ms=NOW)
        stage = state.stage("submit_to_ack")

        self.assertEqual(stage.status, "UNKNOWN")
        self.assertFalse(stage.p95.known)
        self.assertIsNone(stage.p95.value)
        self.assertIn("not 0 ms", stage.p95.reason)
        self.assertEqual(stage.budget_ms.value, 500)

    def test_6_budget_exceeded_is_flagged_per_stage(self) -> None:
        log = BoundedLatencyLog(capacity=50)
        for index in range(6):
            log.record(LatencySample(stage="submit_to_ack", ts=NOW + index, value_ms=900))

        state = evaluate_latency(policy=policy(latency_budget_ms={**{s: 500 for s in LATENCY_STAGES},
                                                                "submit_to_ack": 800}),
                                 log=log, now_ms=NOW)
        self.assertEqual(state.stage("submit_to_ack").status, "BUDGET_EXCEEDED")
        self.assertEqual(state.stage("decision_to_submit").status, "UNKNOWN")

    def test_samples_outside_the_window_are_ignored(self) -> None:
        log = BoundedLatencyLog(capacity=50)
        for index in range(6):
            log.record(LatencySample(stage="submit_to_ack", ts=NOW - 10_000_000, value_ms=10))
        state = evaluate_latency(policy=policy(), log=log, now_ms=NOW)
        self.assertEqual(state.stage("submit_to_ack").sample_count, 0)


class HealthTest(unittest.TestCase):
    def test_3_missing_policy_is_unknown_never_healthy(self) -> None:
        health = evaluate_health(policy=None, source_status={source: "HEALTHY" for source in HEALTH_SOURCES})

        self.assertIs(health.status, ExecutionHealthStatus.UNKNOWN)
        self.assertIn("EXECUTION_POLICY_NOT_CONFIGURED", health.reasons)

    def test_required_unknown_blocks_healthy(self) -> None:
        statuses = {source: "HEALTHY" for source in HEALTH_SOURCES}
        statuses["private_latency"] = "UNKNOWN"
        health = evaluate_health(policy=policy(), source_status=statuses)

        self.assertIsNot(health.status, ExecutionHealthStatus.HEALTHY)
        self.assertTrue(any("private_latency" in reason for reason in health.reasons))

    def test_8_degradation_is_explainable_and_severity_driven(self) -> None:
        """严重度由 policy 的 health_rules 决定（同一来源可配成 DEGRADED 或 BLOCKING）。"""
        statuses = {source: "HEALTHY" for source in HEALTH_SOURCES}
        statuses["rate_limit_state"] = "NEAR_LIMIT"
        degraded = evaluate_health(policy=policy(health_rules=health_rules(
            rate_limit_state=HealthRule(Severity.DEGRADED, True, True))), source_status=statuses)
        self.assertIs(degraded.status, ExecutionHealthStatus.DEGRADED)
        self.assertIn("rate_limit_state:NEAR_LIMIT", degraded.reasons)

        blocked = evaluate_health(policy=policy(), source_status=statuses)
        self.assertIs(blocked.status, ExecutionHealthStatus.BLOCKED)

    def test_info_sources_do_not_downgrade(self) -> None:
        statuses = {source: "HEALTHY" for source in HEALTH_SOURCES}
        statuses["unknown_lost_orders"] = "DEGRADED"
        health = evaluate_health(policy=policy(health_rules=health_rules(
            unknown_lost_orders=HealthRule(Severity.INFO, False, False))), source_status=statuses)

        self.assertIs(health.status, ExecutionHealthStatus.HEALTHY)


class ProjectionTest(unittest.TestCase):
    def projection(self, **overrides: object) -> ExecutionSafetyProjection:
        values: dict[str, object] = {
            "clock": lambda: NOW, "policy": policy(), "rules_provider": rules,
            "rate_facts_provider": lambda: rate_facts(used_weight=100, limit=1_000),
            "exposure_provider": lambda: {"uncertain_exposure": 0.0, "unknown_orders": 0},
        }
        values.update(overrides)
        return ExecutionSafetyProjection(**values)  # type: ignore[arg-type]

    def test_1_venue_limits_reuse_trading_rules(self) -> None:
        state = self.projection().limits()

        self.assertEqual(state.tick_size.value, 0.1)
        self.assertEqual(state.step_size.value, 0.0001)
        self.assertEqual(state.min_notional.value, 50.0)
        # rate/order 限额来自注入的真实事实；交易所未提供的 max_orders 保持 UNKNOWN
        self.assertTrue(state.request_weight_limit.known)
        self.assertFalse(state.max_orders.known)

    def test_2_venue_facts_without_evidence_produce_blocking_blocker(self) -> None:
        projection = self.projection(rate_facts_provider=lambda: None)
        blockers = {blocker.reason_code: blocker for blocker in projection.blockers()}

        self.assertIn("VENUE_FACTS_UNAVAILABLE", blockers)
        self.assertIs(blockers["VENUE_FACTS_UNAVAILABLE"].owner, BlockerOwner.VENUE)
        self.assertIs(blockers["VENUE_FACTS_UNAVAILABLE"].severity, BlockerSeverity.BLOCKING)

    def test_3_projection_without_policy_reports_policy_blocker(self) -> None:
        projection = self.projection(policy=None)
        blockers = projection.blockers()

        self.assertEqual([b.reason_code for b in blockers], ["EXECUTION_POLICY_NOT_CONFIGURED"])
        self.assertIs(projection.health().status, ExecutionHealthStatus.UNKNOWN)

    def test_8_rate_limit_and_latency_blockers_carry_source_refs(self) -> None:
        log = BoundedLatencyLog(capacity=20)
        for index in range(6):
            log.record(LatencySample(stage="submit_to_ack", ts=NOW + index, value_ms=900))
        projection = self.projection(
            latency_log=log,
            rate_facts_provider=lambda: rate_facts(used_weight=990, limit=1_000),
            policy=policy(latency_budget_ms={**{s: 500 for s in LATENCY_STAGES}, "submit_to_ack": 800}))
        blockers = {blocker.reason_code: blocker for blocker in projection.blockers()}

        self.assertIn("RATE_LIMIT_EXHAUSTED", blockers)
        self.assertEqual(blockers["RATE_LIMIT_EXHAUSTED"].source_ref, "execution.rate_limits.requests")
        self.assertIn("SUBMIT_LATENCY_BUDGET_EXCEEDED", blockers)
        self.assertEqual(blockers["SUBMIT_LATENCY_BUDGET_EXCEEDED"].source_ref,
                         "execution.latency.submit_to_ack")

    def test_9_reconciliation_required_and_exposed(self) -> None:
        projection = self.projection(exposure_provider=lambda: {"uncertain_exposure": 0.5, "unknown_orders": 1})
        reconciliation = projection.reconciliation()

        self.assertTrue(reconciliation.required)
        self.assertIn("RECONCILIATION_REQUIRED",
                      {blocker.reason_code for blocker in projection.blockers()})

    def test_19_de_risking_is_never_blocked_by_rate_limits(self) -> None:
        projection = self.projection(rate_facts_provider=lambda: rate_facts(used_weight=1_000, limit=1_000))
        governor = projection.rate_limits()

        self.assertFalse(governor.allows_new_exposure)
        self.assertTrue(governor.allows_de_risking)

    def test_cross_module_boundary_is_respected(self) -> None:
        """执行安全层只读：不得 import 交易执行/策略/风控实现。"""
        for name in ("policy.py", "venue.py", "rate_limit.py", "latency.py", "health.py", "projection.py"):
            source = (PROJECT_ROOT / "execution_safety" / name).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("from ") or stripped.startswith("import "):
                    for root in ("execution.", "strategy", "risk", "live", "connectors.binance.execution"):
                        with self.subTest(module=name, import_line=stripped):
                            self.assertFalse(stripped.startswith(f"from {root}")
                                             or stripped.startswith(f"import {root}"))
