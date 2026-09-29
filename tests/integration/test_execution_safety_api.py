"""P0001.13 集成：执行安全 API（6 只读端点）+ 受控 reconciliation action（SC-9/10/12/13/18/19）。"""

from __future__ import annotations

import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

from actions import ActionAuditLog, ActionGateway, ConfirmationRegistry, HandlerResult
from api.server import create_server
from assistant import AssistantService
from execution_safety import (
    BoundedLatencyLog,
    ExecutionSafetyPolicy,
    ExecutionSafetyProjection,
    HealthRule,
    LATENCY_STAGES,
    LatencySample,
    Severity,
)
from execution_safety.types import HEALTH_SOURCES
from product.types import RuntimeMode
from tests.unit.test_product_snapshot import identity, service

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = 1_000_000


def rules() -> object:
    from connectors.binance.market_data.exchange_info import TradingRules
    return TradingRules(symbol="BTCUSDT", status="TRADING", tick_size=0.1, min_price=100.0,
                        max_price=1_000_000.0, step_size=0.0001, min_qty=0.0001, max_qty=100.0,
                        min_notional=50.0)


def policy() -> ExecutionSafetyPolicy:
    return ExecutionSafetyPolicy(
        near_limit_ratio=0.2, exhausted_ratio=0.05, order_rate_near_limit_ratio=0.25,
        venue_facts_max_age_ms=60_000, latency_budget_ms={stage: 500 for stage in LATENCY_STAGES},
        latency_min_samples=3, latency_window_ms=600_000, reconciliation_required_after_unknown=True,
        health_rules={source: HealthRule(Severity.DEGRADED, True, True) for source in HEALTH_SOURCES})


class ExecutionSafetyApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reconciled: list[str] = []
        from execution_safety.venue import VenueRateLimitFacts
        log = BoundedLatencyLog(capacity=50)
        for stage in LATENCY_STAGES:          # 五阶段都要有样本，否则按设计为 UNKNOWN
            for index in range(4):
                log.record(LatencySample(stage=stage, ts=NOW + index, value_ms=120))
        self.projection = ExecutionSafetyProjection(
            clock=lambda: NOW, policy=policy(), rules_provider=rules,
            rate_facts_provider=lambda: VenueRateLimitFacts(
                request_weight_limit=1_000, order_rate_limit=100, window_ms=60_000, used_weight=100,
                used_orders=5, reset_at_ms=NOW + 30_000, source="test:evidence-confirmed",
                observed_at_ms=NOW),
            latency_log=log,
            private_latency_provider=lambda: "HEALTHY",
            reconciliation_provider=lambda: SimpleNamespace(actions=(1, 2), corrective_actions=(), converged=True),
            reconciliation_requester=lambda: (self.reconciled.append("run"), {"requested": True})[1],
            exposure_provider=lambda: {"uncertain_exposure": 0.0, "unknown_orders": 0})
        self.gateway = ActionGateway(clock=lambda: NOW, confirmations=ConfirmationRegistry(ttl_ms=30_000),
                                    audit=ActionAuditLog(capacity=50))
        snapshot = service(identity=identity(RuntimeMode.REPLAY)).snapshot()
        self.assistant = AssistantService(snapshot_provider=lambda: snapshot, gateway=self.gateway,
                                          execution_safety=lambda: self.projection)
        self.service = service(identity=identity(RuntimeMode.REPLAY),
                               execution_safety=lambda: self.projection,
                               action_gateway=lambda: self.gateway, assistant=lambda: self.assistant)
        self.server = create_server(self.service, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path: str) -> dict:
        with OPENER.open(f"{self.base}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(f"{self.base}{path}", method="POST",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with OPENER.open(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    # ---------------------------------------------------------------- 6 read endpoints

    def test_9_all_execution_endpoints_are_readable(self) -> None:
        health = self.get("/api/v1/execution/health")
        limits = self.get("/api/v1/execution/limits")
        rate = self.get("/api/v1/execution/rate-limits")
        latency = self.get("/api/v1/execution/latency")
        anomalies = self.get("/api/v1/execution/anomalies")
        reconciliation = self.get("/api/v1/execution/reconciliation")

        self.assertEqual(health["health"]["status"], "HEALTHY")
        self.assertEqual(limits["limits"]["source"], "test:evidence-confirmed")
        self.assertEqual(rate["governor"]["request"]["status"], "HEALTHY")
        self.assertEqual(rate["governor"]["allows_de_risking"], True)
        self.assertTrue(latency["latency"]["stages"])
        self.assertIsInstance(anomalies["blockers"], list)
        self.assertEqual(reconciliation["reconciliation"]["state"]["value"], "CONVERGED")

    def test_13_venue_limits_come_from_the_same_source_as_normalization(self) -> None:
        limits = self.get("/api/v1/execution/limits")["limits"]

        self.assertEqual(limits["tick_size"]["value"], 0.1)
        self.assertEqual(limits["step_size"]["value"], 0.0001)
        self.assertEqual(limits["min_notional"]["value"], 50.0)

    def test_2_missing_venue_evidence_is_reported_as_unavailable(self) -> None:
        server = create_server(service(execution_safety=lambda: ExecutionSafetyProjection(
            clock=lambda: NOW, policy=policy(), rules_provider=rules, rate_facts_provider=lambda: None)),
            host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with OPENER.open(f"{base}/api/v1/execution/anomalies", timeout=5) as response:
                blockers = json.loads(response.read().decode("utf-8"))["blockers"]
            reasons = {blocker["reason_code"] for blocker in blockers}
            self.assertIn("VENUE_FACTS_UNAVAILABLE", reasons)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_3_policy_missing_makes_health_unknown_and_endpoints_503(self) -> None:
        projection = ExecutionSafetyProjection(clock=lambda: NOW, rules_provider=rules)
        server = create_server(service(execution_safety=lambda: projection), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with OPENER.open(f"{base}/api/v1/execution/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))["health"]
            self.assertEqual(health["status"], "UNKNOWN")
            self.assertIn("EXECUTION_POLICY_NOT_CONFIGURED", health["reasons"])
            for path in ("/api/v1/execution/rate-limits", "/api/v1/execution/latency"):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        OPENER.open(f"{base}{path}", timeout=5)
                    self.assertEqual(ctx.exception.code, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_unwired_projection_is_503(self) -> None:
        server = create_server(service(), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                OPENER.open(f"{base}/api/v1/execution/health", timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_18_no_new_write_path_was_introduced(self) -> None:
        """SC-18：执行端点全是只读；唯一 POST 仍是 Action Plane。"""
        for path in ("/api/v1/execution/health", "/api/v1/execution/reconciliation"):
            with self.subTest(path=path):
                status, _ = self.post(path, {})
                self.assertEqual(status, 405)
        manifest = self.get("/api/v1/capabilities")
        self.assertEqual(manifest["api"]["write"], "unavailable_by_design")
        for path in ("/api/v1/execution/health", "/api/v1/execution/rate-limits"):
            self.assertIn(path, manifest["api"]["read"])

    # ---------------------------------------------------------------- controlled reconciliation

    def test_11_reconciliation_action_requires_confirmation_and_runs(self) -> None:
        manifest = {entry["action_id"]: entry for entry in self.get("/api/v1/actions")["actions"]}
        self.assertTrue(manifest["runtime.request_reconciliation"]["available"])
        self.assertTrue(manifest["runtime.request_reconciliation"]["confirmation_required"])

        status, pending = self.post("/api/v1/actions/runtime.request_reconciliation", {"requested_by": "agent"})
        self.assertEqual(status, 409)
        self.assertEqual(pending["action"]["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(self.reconciled, [])          # 未确认 ⇒ 既有 Owner 未被调用

        confirmed_status, confirmed = self.post(
            "/api/v1/actions/runtime.request_reconciliation",
            {"confirmation": pending["action"]["confirmation_id"]})
        self.assertEqual(confirmed_status, 200)
        self.assertEqual(confirmed["action"]["status"], "SUCCEEDED")
        self.assertEqual(self.reconciled, ["run"])

    def test_10_assistant_reads_execution_facts_and_suggests_reconciliation_when_required(self) -> None:
        payload = self.get("/api/v1/assistant/context?surface=system")
        context = payload["context"]

        self.assertEqual(context["execution_health"]["value"], "HEALTHY")
        self.assertTrue(context["rate_limit_state"]["known"])
        self.assertEqual(context["uncertain_exposure"]["value"], 0.0)
        read_actions = {entry["action_id"] for entry in payload["suggested_actions"]}
        self.assertIn("execution.health", read_actions)

    def test_10_reconciliation_suggestion_is_prioritised_when_required(self) -> None:
        requiring = ExecutionSafetyProjection(
            clock=lambda: NOW, policy=policy(), rules_provider=rules,
            rate_facts_provider=lambda: self.projection.rate_facts_provider(),
            exposure_provider=lambda: {"uncertain_exposure": 0.5, "unknown_orders": 1},
            reconciliation_requester=lambda: {"requested": True})
        assistant = AssistantService(
            snapshot_provider=lambda: service(execution_safety=lambda: requiring).snapshot(),
            gateway=self.gateway, execution_safety=lambda: requiring)
        context = assistant.context(surface="system")
        # blocker 存在 ⇒ 上下文可见（suggested 由 Manifest 决定，Gateway 之外的排序规则在单测覆盖）
        self.assertTrue(any(blocker.endswith("RECONCILIATION_REQUIRED") for blocker in context.active_blockers))

    def test_16_readiness_and_execution_health_stay_separate(self) -> None:
        snapshot = self.get("/api/v1/snapshot")
        self.assertIn("readiness", snapshot)
        self.assertIn("execution_safety", snapshot)
        self.assertNotEqual(snapshot["readiness"]["status"], None)
        self.assertTrue(snapshot["execution_safety"]["health_status"]["known"])

    def test_15_run_summary_accepts_execution_metrics(self) -> None:
        from reports import build_run_summary
        summary = build_run_summary(identity=identity(RuntimeMode.REPLAY), run_id="rt-1", started_at=1_000,
                                    execution_metrics={"unknown_orders": 0, "lost_orders": 0,
                                                       "reconciliation_count": 1})
        self.assertEqual(summary.metrics["execution.unknown_orders"], 0)
        self.assertEqual(summary.metrics["execution.reconciliation_count"], 1)
