"""F-12/F-15 ops posture：四层健康互相独立 + network/logging/retention posture。"""

from __future__ import annotations

import unittest

from runtime.ops import (NetworkPosture, build_health_split, build_ops_payload,
                         build_retention_posture)
from storage.retention import RetentionPolicy


class HealthSplitTest(unittest.TestCase):
    def test_process_live_does_not_imply_trade_ready(self) -> None:
        health = build_health_split(runtime_state="RUNNING", runtime_detail="running",
                                    readiness_status="blocked",
                                    readiness_reasons=("HISTORICAL_DRAWDOWN_UNKNOWN",),
                                    execution_health="DEGRADED")
        self.assertTrue(health.process_live)
        self.assertEqual(health.trade_readiness, "blocked")
        self.assertEqual(health.execution_health, "DEGRADED")
        self.assertIsNotNone(health.operational_warning)
        self.assertNotIn("READY", health.trade_readiness)

    def test_readiness_not_evaluated_is_not_ready(self) -> None:
        health = build_health_split(runtime_state="STARTING", runtime_detail="x")
        self.assertEqual(health.trade_readiness, "NOT_EVALUATED")
        self.assertEqual(health.execution_health, "UNKNOWN")

    def test_ready_readiness_has_no_warning(self) -> None:
        health = build_health_split(runtime_state="RUNNING", runtime_detail="x",
                                    readiness_status="ready", execution_health="HEALTHY")
        self.assertIsNone(health.operational_warning)


class NetworkAndRetentionTest(unittest.TestCase):
    def test_network_posture_payload(self) -> None:
        posture = NetworkPosture(bind_host="0.0.0.0", loopback=False, allow_non_loopback=True,
                                 auth_required=True, auth_token_ref="env:PROBEX_API_TOKEN")
        payload = posture.to_payload()
        self.assertEqual(payload["bind_host"], "0.0.0.0")
        self.assertFalse(payload["loopback"])
        self.assertTrue(payload["auth_required"])
        self.assertEqual(payload["auth_token_ref"], "env:PROBEX_API_TOKEN")

    def test_retention_posture_unbounded_vs_bounded(self) -> None:
        unbounded = build_retention_posture(policy=RetentionPolicy(), runs_total=3, index_bytes=10,
                                            record_bytes=20, event_store_bytes=30, audit_entries=1,
                                            audit_capacity=500, latency_samples=0,
                                            latency_capacity=2000, logging_bounded=False)
        self.assertFalse(unbounded["bounded"])
        self.assertFalse(unbounded["stores"]["run_registry"]["bounded"])
        self.assertIsNone(unbounded["last_prune"])
        bounded = build_retention_posture(policy=RetentionPolicy(run_max_runs=5), runs_total=3,
                                          index_bytes=10, record_bytes=20, event_store_bytes=30,
                                          audit_entries=1, audit_capacity=500, latency_samples=0,
                                          latency_capacity=2000, logging_bounded=True)
        self.assertTrue(bounded["bounded"])
        self.assertTrue(bounded["stores"]["run_registry"]["bounded"])
        # event store 永不自动删除（active replay source）
        self.assertFalse(bounded["stores"]["event_store"]["applied"])

    def test_ops_payload_contains_four_layers(self) -> None:
        health = build_health_split(runtime_state="RUNNING", runtime_detail="x",
                                    readiness_status="blocked", execution_health="DEGRADED")
        payload = build_ops_payload(process_started_at_ms=123, network=NetworkPosture(
            bind_host="127.0.0.1", loopback=True, allow_non_loopback=False, auth_required=False),
            health=health, logging={"bounded": False}, retention={"bounded": False}, now_ms=456)
        for key in ("process_live", "runtime_state", "trade_readiness", "execution_health",
                    "network", "logging", "retention", "ts"):
            with self.subTest(key=key):
                self.assertIn(key, payload)
        self.assertTrue(payload["process_live"])
        self.assertEqual(payload["trade_readiness"], "blocked")
        self.assertNotEqual(payload["runtime_state"], payload["trade_readiness"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
