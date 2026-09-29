"""P0001.12.3 集成：Action Plane API（SC-4/5/6/7/9/10/11/12/13）。"""

from __future__ import annotations

import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request

from actions import ActionAuditLog, ActionGateway, ActionOutcomeUnknown, ConfirmationRegistry, HandlerResult
from api.server import create_server
from assistant import AssistantService
from product.types import RuntimeMode
from tests.unit.test_product_snapshot import identity, service

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class ActionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stopped: list[str] = []
        self.gateway = ActionGateway(clock=lambda: 1_000, confirmations=ConfirmationRegistry(ttl_ms=30_000),
                                    audit=ActionAuditLog(capacity=50))
        self.gateway.register("replay.control", lambda request, ctx: HandlerResult(
            result={"verb": request.parameters.get("verb")}, fact_refs=("replay:command",)))
        self.gateway.register("report.generate", lambda request, ctx: HandlerResult(
            result={"run_id": request.parameters.get("run_id", "n/a"), "format": "json"}))
        self.gateway.register("runtime.stop_replay", self._stop_replay)
        snapshot = service(identity=identity(RuntimeMode.REPLAY)).snapshot()
        self.assistant = AssistantService(snapshot_provider=lambda: snapshot, gateway=self.gateway)
        self.service = service(identity=identity(RuntimeMode.REPLAY),
                               action_gateway=lambda: self.gateway, assistant=lambda: self.assistant)
        self.server = create_server(self.service, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _stop_replay(self, request: object, ctx: object) -> HandlerResult:
        self.stopped.append(getattr(ctx, "runtime_id", "?"))
        return HandlerResult(result={"stopped": True}, fact_refs=("runtime:stop",))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path: str) -> dict:
        with OPENER.open(f"{self.base}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, action_id: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(f"{self.base}/api/v1/actions/{action_id}", method="POST",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with OPENER.open(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    # ---------------------------------------------------------------- manifest

    def test_10_manifest_shows_capital_as_unavailable(self) -> None:
        manifest = self.get("/api/v1/actions")
        capital = [entry for entry in manifest["actions"] if entry["level"] == "L3_CAPITAL"]

        self.assertTrue(capital)
        for entry in capital:
            with self.subTest(action=entry["action_id"]):
                self.assertFalse(entry["available"])
                self.assertEqual(entry["availability"], "UNAVAILABLE_BY_DESIGN")
                self.assertTrue(entry["unavailable_reason"])
        self.assertEqual(manifest["levels"]["L3_CAPITAL"], "unavailable_by_design")

    def test_manifest_reports_registered_handlers(self) -> None:
        manifest = {entry["action_id"]: entry for entry in self.get("/api/v1/actions")["actions"]}

        self.assertTrue(manifest["replay.control"]["available"])
        self.assertFalse(manifest["runtime.request_reconciliation"]["available"])
        self.assertEqual(manifest["runtime.request_reconciliation"]["availability"],
                         "UNAVAILABLE_NO_ENTRY_POINT")

    # ---------------------------------------------------------------- L1 product

    def test_4_replay_control_goes_through_the_gateway(self) -> None:
        status, payload = self.post("replay.control", {"parameters": {"verb": "pause"},
                                                       "requested_by": "cli"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["action"]["status"], "SUCCEEDED")
        self.assertEqual(payload["action"]["result"]["verb"], "pause")
        self.assertEqual(payload["action"]["resulting_fact_refs"], ["replay:command"])

    def test_5_report_generation_is_a_product_action(self) -> None:
        status, payload = self.post("report.generate", {"parameters": {"run_id": "rt-1"}})

        self.assertEqual(status, 200)
        self.assertEqual(payload["action"]["result"]["run_id"], "rt-1")

    # ---------------------------------------------------------------- L2 runtime

    def test_6_7_real_runtime_action_requires_and_honours_confirmation(self) -> None:
        status, payload = self.post("runtime.stop_replay", {"requested_by": "agent"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["action"]["status"], "CONFIRMATION_REQUIRED")
        confirmation = payload["action"]["confirmation_id"]
        self.assertEqual(self.stopped, [])           # 未确认 ⇒ handler 绝不被调用

        confirmed_status, confirmed = self.post("runtime.stop_replay", {"confirmation": confirmation})
        self.assertEqual(confirmed_status, 200)
        self.assertEqual(confirmed["action"]["status"], "SUCCEEDED")
        self.assertEqual(len(self.stopped), 1)

    def test_7_replayed_confirmation_is_refused(self) -> None:
        _, first = self.post("runtime.stop_replay", {})
        confirmation = first["action"]["confirmation_id"]
        self.post("runtime.stop_replay", {"confirmation": confirmation})
        status, second = self.post("runtime.stop_replay", {"confirmation": confirmation})

        self.assertEqual(status, 409)
        self.assertEqual(second["action"]["status"], "REFUSED")
        self.assertIn("CONFIRMATION_INVALID", second["action"]["reason_code"])
        self.assertEqual(len(self.stopped), 1)

    def test_capital_action_is_refused_with_409(self) -> None:
        status, payload = self.post("capital.place_order",
                                    {"parameters": {"symbol": "BTCUSDT", "side": "buy"}})

        self.assertEqual(status, 409)
        self.assertEqual(payload["action"]["status"], "REFUSED")
        self.assertEqual(payload["action"]["reason_code"], "UNAVAILABLE_BY_DESIGN")

    def test_13_unknown_outcome_is_preserved(self) -> None:
        self.gateway.register("runtime.stop_replay",
                              lambda request, ctx: (_ for _ in ()).throw(ActionOutcomeUnknown("unknown")))
        _, pending = self.post("runtime.stop_replay", {})
        status, payload = self.post("runtime.stop_replay",
                                    {"confirmation": pending["action"]["confirmation_id"]})

        self.assertEqual(status, 202)
        self.assertEqual(payload["action"]["status"], "UNKNOWN")

    def test_9_audit_records_every_invocation(self) -> None:
        self.post("replay.control", {"parameters": {"verb": "play"}})
        self.post("capital.place_order", {})
        audit = self.get("/api/v1/actions/audit")

        self.assertEqual([entry["status"] for entry in audit["entries"]], ["SUCCEEDED", "REFUSED"])
        self.assertTrue(all(entry["parameters_fingerprint"].startswith("sha256:")
                            for entry in audit["entries"]))

    # ---------------------------------------------------------------- assistant

    def test_1_assistant_context_endpoint(self) -> None:
        payload = self.get("/api/v1/assistant/context?surface=activity&order=probex-s1-1")

        self.assertEqual(payload["context"]["surface"], "activity")
        self.assertEqual(payload["context"]["selected_order_id"]["value"], "probex-s1-1")
        self.assertEqual(payload["context"]["runtime"]["mode"], "REPLAY")
        suggested = {entry["action_id"] for entry in payload["suggested_actions"]}
        self.assertIn("replay.control", suggested)
        self.assertNotIn("capital.place_order", suggested)

    def test_11_ui_cli_and_agent_share_the_same_gateway(self) -> None:
        """没有 AI 专用后门：CLI 也走 HTTP Action Plane，不直接持有 Gateway 实现。"""
        source = (PROJECT_ROOT / "cli" / "main.py").read_text(encoding="utf-8")

        self.assertIn("ACTIONS_PATH", source)
        self.assertNotIn("ActionGateway(", source)

    def test_action_gateway_absence_is_503(self) -> None:
        server = create_server(service(), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                OPENER.open(f"{base}/api/v1/actions", timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
