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


BOUNDS = {"projection.window_ms": 600_000, "projection.bucket_ms": 1_000,
          "projection.max_points": 200, "projection.price_levels": 5,
          "projection.history_capacity": 500, "projection.view_depth": 10}


class AssemblyProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probex-profile-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config_file(self, **overrides: object) -> str:
        import json as _json

        path = self.tmp / "profile.json"
        path.write_text(_json.dumps({**BOUNDS, **overrides}), encoding="utf-8")
        return str(path)

    def test_replay_is_the_default_mode_and_bind_is_loopback(self) -> None:
        profile = build_profile_from_args(["--symbol", "BTCUSDT", "--mode", "replay",
                                           "--event-store", str(self.tmp / "events.jsonl"),
                                           "--config-file", self.config_file()])

        self.assertIs(profile.mode, RuntimeMode.REPLAY)
        self.assertEqual(profile.host, "127.0.0.1")
        self.assertIn(RuntimeMode.LIVE, ALLOWED_MODES)          # 可用但**不是**默认
        self.assertIsNotNone(profile.feed)

    def test_replay_requires_an_event_store_and_explicit_bounds(self) -> None:
        with self.assertRaises(AssemblyError):
            build_profile_from_args(["--symbol", "BTCUSDT"])                     # 无 event store
        with self.assertRaises(AssemblyError):
            build_profile_from_args(["--symbol", "BTCUSDT", "--mode", "paper",
                                     "--event-store", str(self.tmp / "events.jsonl")])   # 无 bounds

    def test_testnet_and_live_require_explicit_mode(self) -> None:
        self.assertIs(build_profile_from_args(["--symbol", "BTCUSDT", "--mode", "testnet"]).mode,
                      RuntimeMode.TESTNET)
        self.assertIs(build_profile_from_args(["--symbol", "BTCUSDT", "--mode", "live"]).mode,
                      RuntimeMode.LIVE)
        self.assertIsNone(build_profile_from_args(["--symbol", "BTCUSDT", "--mode", "testnet"]).feed)

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


class RealFeedSlice2Test(ProductRuntimeTest):
    """Slice 2：REPLAY/PAPER 真实 feed（event store → ReplaySource → MarketBook/FeatureEngine → 投影）。"""

    def feed_profile(self, mode: RuntimeMode):
        import json as _json

        from tests import scenarios
        from tests.support import write_store
        from runtime.assembly import FeedProfile

        store = self.tmp / "events.jsonl"
        write_store(store, scenarios.reference_events())
        (self.tmp / "profile.json").write_text(_json.dumps({
            "projection.window_ms": 600_000, "projection.bucket_ms": 1_000,
            "projection.max_points": 200, "projection.price_levels": 5,
            "projection.history_capacity": 500, "projection.view_depth": 10}), encoding="utf-8")
        return RuntimeProfile(symbol="BTCUSDT", config_entries=self.cfg, mode=mode,
                              run_registry_dir=str(self.tmp / f"runs-{mode.value}"),
                              feed=FeedProfile(event_store=str(store), window_ms=600_000,
                                               bucket_ms=1_000, max_points=200, price_levels=5,
                                               history_capacity=500, view_depth=10))

    def test_replay_run_consumes_real_events_and_is_visible_while_running(self) -> None:
        runtime = ProductRuntime(profile=self.feed_profile(RuntimeMode.REPLAY))
        runtime.start()
        deadline = __import__("time").time() + 5
        while __import__("time").time() < deadline and not runtime._feed_stats.get("completed"):  # noqa: SLF001
            __import__("time").sleep(0.05)
        try:
            runs = runtime.service.run_registry_view().list()
            self.assertEqual([r.run_id for r in runs], [runtime.run_id])
            self.assertIs(runs[0].status, RunStatus.RUNNING)          # F-10：活跃 ⇒ RUNNING
            counts = runtime.service.market_history_view().counts
            self.assertGreater(counts["states"], 0)                    # 真实事实进入投影
            self.assertGreater(counts["snapshots"], 0)
            timeline = runtime.service.snapshot().market                # 投影可用（非 UNKNOWN）
            self.assertTrue(timeline.best_bid.known)
        finally:
            runtime.stop()

        record = runtime.service.run_registry_view().load(runtime.run_id)
        self.assertIs(record.status, RunStatus.COMPLETED)
        self.assertIsNone(runtime.service.run_registry_view().active_run_id())

    def test_paper_run_uses_the_existing_paper_broker_without_fabricating_orders(self) -> None:
        runtime = ProductRuntime(profile=self.feed_profile(RuntimeMode.PAPER))
        runtime.start()
        deadline = __import__("time").time() + 5
        while __import__("time").time() < deadline and not runtime._feed_stats.get("completed"):  # noqa: SLF001
            __import__("time").sleep(0.05)
        try:
            provider = runtime._feed_provider                                   # noqa: SLF001
            self.assertIsNotNone(provider.paper_broker)                         # 真实 paper 链路
            self.assertIsNotNone(provider.paper_manager)
            self.assertGreater(runtime.service.market_history_view().counts["states"], 0)
            self.assertEqual(runtime.service.snapshot().execution.active_orders, ())   # 未伪造订单
        finally:
            runtime.stop()

    def test_replay_and_paper_share_the_same_market_path(self) -> None:
        replay = self.feed_profile(RuntimeMode.REPLAY)
        paper = self.feed_profile(RuntimeMode.PAPER)
        self.assertEqual(replay.feed.event_store, paper.feed.event_store)       # 同一市场数据来源
        self.assertIsNot(replay.mode, paper.mode)
