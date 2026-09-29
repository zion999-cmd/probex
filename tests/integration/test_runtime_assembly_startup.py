"""Closure Slice 1（F-01 / F-11）：真实产品装配入口 —— 不使用 test harness 手工构造。"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from actions import ActionAvailability, ActionLevel
from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from reports.types import RunStatus
from runtime.assembly import ALLOWED_MODES, AssemblyError, ProductRuntime, RuntimeProfile, \
    build_profile_from_args
from runtime.state import RuntimeState

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AssemblyProfileTest(unittest.TestCase):
    def test_default_mode_is_replay_and_default_bind_is_loopback(self) -> None:
        profile = build_profile_from_args(["--symbol", "BTCUSDT"])

        self.assertIs(profile.mode, RuntimeMode.REPLAY)
        self.assertEqual(profile.host, "127.0.0.1")
        self.assertIn(RuntimeMode.LIVE, ALLOWED_MODES)          # 可用但**不是**默认

    def test_testnet_and_live_require_explicit_mode(self) -> None:
        self.assertIs(build_profile_from_args(["--symbol", "BTCUSDT", "--mode", "testnet"]).mode,
                      RuntimeMode.TESTNET)
        self.assertIs(build_profile_from_args(["--symbol", "BTCUSDT", "--mode", "live"]).mode,
                      RuntimeMode.LIVE)
        self.assertNotEqual(build_profile_from_args(["--symbol", "BTCUSDT"]).mode, RuntimeMode.LIVE)

    def test_profile_refuses_missing_symbol_or_config(self) -> None:
        with self.assertRaises(AssemblyError):
            RuntimeProfile(symbol="", config_entries=())
        with self.assertRaises(AssemblyError):
            RuntimeProfile(symbol="BTCUSDT", config_entries=())


class ProductRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probex-assembly-"))
        self.cfg = (ConfigEntry(name="symbol", source=ConfigSource.CLI, value=Fact.of("BTCUSDT")),
                    ConfigEntry(name="mode", source=ConfigSource.CLI, value=Fact.of("replay")))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runtime(self, **overrides: object) -> ProductRuntime:
        values: dict[str, object] = {"symbol": "BTCUSDT", "config_entries": self.cfg,
                                     "run_registry_dir": str(self.tmp / "runs")}
        values.update(overrides)
        return ProductRuntime(profile=RuntimeProfile(**values))  # type: ignore[arg-type]

    def test_service_is_built_by_the_assembly_not_by_test_helpers(self) -> None:
        runtime = self.runtime()

        snap = runtime.service.snapshot()
        self.assertEqual(snap.runtime.runtime_id, runtime.identity.runtime_id)
        self.assertEqual(snap.runtime.mode, RuntimeMode.REPLAY)
        self.assertTrue(snap.config.config_id.known)

    def test_start_projects_runtime_state_running_and_creates_a_run(self) -> None:
        runtime = self.runtime()
        status = runtime.start()
        snap = runtime.service.snapshot()

        self.assertIs(status.state, RuntimeState.RUNNING)
        self.assertEqual(snap.health.runtime_state.value, "RUNNING")
        self.assertEqual(snap.health.runtime_run_id.value, runtime.run_id)
        self.assertIs(snap.health.runtime_quoting.value, False)     # 无行情源 ⇒ 不谎报在报价
        self.assertTrue(snap.health.runtime_detail.value)
        self.assertIsNotNone(runtime.service.run_registry_view().load(runtime.run_id))

    def test_graceful_stop_completes_the_run_and_sets_stopped(self) -> None:
        runtime = self.runtime()
        runtime.start()
        status = runtime.stop()
        record = runtime.service.run_registry_view().load(runtime.run_id)

        self.assertIs(status.state, RuntimeState.STOPPED)
        self.assertIs(record.status, RunStatus.COMPLETED)
        self.assertEqual(runtime.service.snapshot().health.runtime_state.value, "STOPPED")

    def test_failure_marks_failed_and_leaves_run_incomplete(self) -> None:
        runtime = self.runtime()
        runtime.start()
        status = runtime.fail("simulated crash")
        record = runtime.service.run_registry_view().load(runtime.run_id)

        self.assertIs(status.state, RuntimeState.FAILED)
        self.assertIs(record.status, RunStatus.INCOMPLETE)
        self.assertNotEqual(record.status, RunStatus.COMPLETED)

    def test_baseline_actions_are_registered_and_capital_stays_unavailable(self) -> None:
        runtime = self.runtime()
        runtime.start()
        manifest = {entry["action_id"]: entry for entry in runtime.gateway.manifest()}

        for action_id in ("inspect.snapshot", "explain.entity", "compare.runs", "query.blockers",
                          "query.health", "query.raw_facts", "navigate.surface", "select.entity",
                          "report.generate", "view.configure"):
            with self.subTest(action=action_id):
                self.assertTrue(manifest[action_id]["available"], action_id)
        capital = [entry for entry in manifest.values() if entry["level"] == ActionLevel.CAPITAL.value]
        self.assertTrue(capital)
        self.assertTrue(all(entry["availability"] == ActionAvailability.UNAVAILABLE_BY_DESIGN.value
                            for entry in capital))
        self.assertNotIn("capital.place_order", runtime.gateway.registered)

    def test_replay_control_is_only_registered_for_replay(self) -> None:
        runtime = self.runtime()
        runtime.with_replay_control(object())
        self.assertIn("replay.control", runtime.gateway.registered)

        paper = self.runtime(mode=RuntimeMode.PAPER)
        with self.assertRaises(AssemblyError):
            paper.with_replay_control(object())


class AssemblyHttpTest(ProductRuntimeTest):
    """真实 HTTP 启动（loopback 临时端口；不使用任何 test harness 组装 service）。"""

    def test_snapshot_ui_actions_and_audit_over_real_http(self) -> None:
        runtime = self.runtime()
        runtime.start()
        server = runtime.create_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = runtime.server_url()
        try:
            with OPENER.open(f"{base}/api/v1/snapshot", timeout=5) as response:
                snapshot = json.loads(response.read().decode("utf-8"))
            self.assertEqual(snapshot["runtime"]["runtime_id"], runtime.identity.runtime_id)
            self.assertEqual(snapshot["health"]["runtime_state"]["value"], "RUNNING")

            with OPENER.open(f"{base}/", timeout=5) as response:
                html = response.read().decode("utf-8")
            self.assertIn("Probex", html)

            with OPENER.open(f"{base}/api/v1/actions", timeout=5) as response:
                manifest = json.loads(response.read().decode("utf-8"))
            available = {entry["action_id"] for entry in manifest["actions"] if entry["available"]}
            self.assertIn("inspect.snapshot", available)

            request = urllib.request.Request(f"{base}/api/v1/actions/inspect.snapshot", method="POST",
                                             data=b"{}", headers={"Content-Type": "application/json"})
            with OPENER.open(request, timeout=5) as response:
                action = json.loads(response.read().decode("utf-8"))["action"]
            self.assertEqual(action["status"], "SUCCEEDED")

            with OPENER.open(f"{base}/api/v1/actions/audit", timeout=5) as response:
                audit = json.loads(response.read().decode("utf-8"))
            self.assertTrue(audit["entries"])

            capital = urllib.request.Request(f"{base}/api/v1/actions/capital.place_order", method="POST",
                                             data=b"{}", headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                OPENER.open(capital, timeout=5)
            self.assertEqual(ctx.exception.code, 409)
        finally:
            runtime.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
