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


class AccountingFactsStep4Test(ProductRuntimeTest):
    """Step 4 守卫：accounting 接线后 snapshot 必须 200；callable/对象不得进入产品 schema。"""

    def accounting_profile(self):
        from runtime.accounting_facts import AccountingFactsProvider
        from runtime.assembly import RuntimeProfile

        return RuntimeProfile(symbol="BTCUSDT",
                              config_entries=self.cfg + (
                                  ConfigEntry(name="accounting.initial_balance",
                                              source=ConfigSource.CLI, value=Fact.of(10_000.0)),
                                  ConfigEntry(name="accounting.timeline_capacity",
                                              source=ConfigSource.CLI, value=Fact.of(50))),
                              run_registry_dir=str(self.tmp / "runs-acct"))

    def test_snapshot_is_200_when_accounting_is_wired(self) -> None:
        runtime = ProductRuntime(profile=self.accounting_profile())
        runtime.start()
        try:
            snap = runtime.service.snapshot()
            self.assertTrue(snap.portfolio.equity.known)
            self.assertEqual(snap.portfolio.equity.value, 10_000.0)
            self.assertEqual(snap.health.accounting.value, "HEALTHY")
        finally:
            runtime.stop()

    def test_snapshot_serializes_without_callables_or_unknown_types(self) -> None:
        from product.serialization import snapshot_to_json

        runtime = ProductRuntime(profile=self.accounting_profile())
        runtime.start()
        try:
            text = snapshot_to_json(runtime.service.snapshot())
        finally:
            runtime.stop()
        self.assertIn('"equity"', text)
        self.assertNotIn("method", text)

    def test_equity_as_method_is_called_explicitly(self) -> None:
        from runtime.accounting_facts import AccountingFactsProvider

        class MethodEquity:
            @property
            def balance(self) -> float:
                return 500.0

            def equity(self) -> float:
                return 750.0

            def realized_trade_pnl(self) -> float:
                return 12.5

            def unrealized_pnl(self) -> float:
                return 5.0

            def position(self, symbol: str) -> object:  # noqa: ARG002
                return type("P", (), {"qty": 0.0})()

            @property
            def baseline_applied(self) -> bool:
                return True

            def mark_timestamp(self, symbol: str):  # noqa: ARG002
                return None

        facts = AccountingFactsProvider(accounting=MethodEquity(), symbol="BTCUSDT",
                                       clock=lambda: 1_000).facts()
        self.assertTrue(facts.equity.known)
        self.assertEqual(facts.equity.value, 750.0)
        self.assertEqual(facts.unrealized_pnl.value, 5.0)
        self.assertEqual(facts.baseline_state.value, "APPLIED")

    def test_unsupported_types_become_unknown_and_do_not_raise(self) -> None:
        from runtime.accounting_facts import AccountingFactsProvider
        from product.types import Fact

        class Hostile:
            equity = lambda self: object()          # noqa: E731 - 返回对象实例（不允许进入 schema）

            @property
            def balance(self) -> Fact:
                return Fact.of(1.0)                 # 返回 Fact 实例（非标量；同样不允许）

            def position(self, symbol: str):        # noqa: ARG002
                raise RuntimeError("no position")

        facts = AccountingFactsProvider(accounting=Hostile(), symbol="BTCUSDT",
                                        clock=lambda: 1_000).facts()
        self.assertFalse(facts.equity.known)
        self.assertIn("unsupported type", facts.equity.reason)
        self.assertFalse(facts.available_balance.known)
        self.assertTrue(facts.anomalies)

    def test_unknown_facts_still_produce_a_valid_snapshot(self) -> None:
        from product.serialization import snapshot_to_json

        class Empty:
            def position(self, symbol: str) -> object:  # noqa: ARG002
                return type("P", (), {"qty": None})()

            def mark_timestamp(self, symbol: str):       # noqa: ARG002
                return None

        from runtime.accounting_facts import AccountingFactsProvider

        runtime = ProductRuntime(profile=self.accounting_profile())
        runtime.start()
        try:
            runtime._accounting_provider = AccountingFactsProvider(  # noqa: SLF001
                accounting=Empty(), symbol="BTCUSDT", clock=lambda: 1_000)
            text = snapshot_to_json(runtime.service.snapshot())
        finally:
            runtime.stop()
        self.assertIn('"known": false', text)


class AccountSamplingStep4bTest(ProductRuntimeTest):
    """Step 4b 守卫：account sampling 只转发既有事实；失败不打断 feed；未知字段不变 0。"""

    def feed_profile_with_accounting(self, *, hostile: bool = False):
        import json as _json

        from runtime.assembly import FeedProfile
        from tests import scenarios
        from tests.support import write_store

        store = self.tmp / "events.jsonl"
        write_store(store, scenarios.reference_events())
        (self.tmp / "profile.json").write_text(_json.dumps({
            "projection.window_ms": 600_000, "projection.bucket_ms": 1_000,
            "projection.max_points": 200, "projection.price_levels": 5,
            "projection.history_capacity": 500, "projection.view_depth": 10}), encoding="utf-8")
        return RuntimeProfile(symbol="BTCUSDT", config_entries=self.cfg + (
            ConfigEntry(name="accounting.initial_balance", source=ConfigSource.CLI, value=Fact.of(10_000.0)),
            ConfigEntry(name="accounting.timeline_capacity", source=ConfigSource.CLI, value=Fact.of(100))),
            mode=RuntimeMode.REPLAY, run_registry_dir=str(self.tmp / "runs-acct-feed"),
            feed=FeedProfile(event_store=str(store), window_ms=600_000, bucket_ms=1_000,
                             max_points=200, price_levels=5, history_capacity=500, view_depth=10))

    def feed_profile(self, mode: RuntimeMode):
        """无 accounting 的 feed profile（用于"未接线语义不变"的守卫）。"""
        import json as _json

        from runtime.assembly import FeedProfile
        from tests import scenarios
        from tests.support import write_store

        store = self.tmp / "events-plain.jsonl"
        write_store(store, scenarios.reference_events())
        (self.tmp / "profile-plain.json").write_text(_json.dumps({
            "projection.window_ms": 600_000, "projection.bucket_ms": 1_000,
            "projection.max_points": 200, "projection.price_levels": 5,
            "projection.history_capacity": 500, "projection.view_depth": 10}), encoding="utf-8")
        return RuntimeProfile(symbol="BTCUSDT", config_entries=self.cfg, mode=mode,
                              run_registry_dir=str(self.tmp / f"runs-plain-{mode.value}"),
                              feed=FeedProfile(event_store=str(store), window_ms=600_000, bucket_ms=1_000,
                                               max_points=200, price_levels=5, history_capacity=500,
                                               view_depth=10))

    def provider_stats(self, runtime) -> dict:
        provider = runtime._feed_provider                                   # noqa: SLF001
        return provider.stats if provider is not None else {}

    def run_until_done(self, runtime) -> None:
        deadline = __import__("time").time() + 8
        while __import__("time").time() < deadline and not self.provider_stats(runtime).get("completed"):
            __import__("time").sleep(0.05)

    def test_portfolio_timeline_gets_points_and_matches_accounting_equity(self) -> None:
        runtime = ProductRuntime(profile=self.feed_profile_with_accounting())
        runtime.start()
        self.run_until_done(runtime)
        try:
            samples = runtime.service.account_timeline_view().samples()
            self.assertGreater(len(samples), 0)
            self.assertEqual(runtime.service.snapshot().portfolio.equity.value,
                             runtime._accounting_provider.facts().equity.value)  # noqa: SLF001
            self.assertEqual(samples[-1].equity, 10_000.0)
            # F1：已知空仓 ⇒ position_qty = 0（事实，不是伪造）；真正未知的是 exposure
            self.assertEqual(samples[-1].position_qty, 0.0)
            self.assertIsNone(samples[-1].exposure_total)
        finally:
            runtime.stop()

    def test_unknown_account_fields_stay_unknown_in_the_projection(self) -> None:
        from product.account_timeline import project_account_timeline
        from product.market_projection import MarketProjectionConfig

        runtime = ProductRuntime(profile=self.feed_profile_with_accounting())
        runtime.start()
        self.run_until_done(runtime)
        try:
            timeline = project_account_timeline(
                runtime.service.account_timeline_view().samples(),
                config=MarketProjectionConfig(window_ms=600_000, bucket_ms=1_000, max_points=200,
                                              price_levels=5))
            self.assertTrue(timeline.points)
            point = timeline.points[-1]
            self.assertTrue(point.equity.known)
            # F1：position_qty 是已知 flat(0)；未知字段（exposure）继续保持 UNKNOWN
            self.assertTrue(point.position_qty.known)
            self.assertEqual(point.position_qty.value, 0.0)
            self.assertFalse(point.exposure_total.known)
            self.assertIsNone(point.exposure_total.value)
        finally:
            runtime.stop()

    def test_account_provider_failure_does_not_break_the_feed(self) -> None:
        from unittest import mock

        from runtime import provider as provider_module

        runtime = ProductRuntime(profile=self.feed_profile_with_accounting())

        def hostile(now_ms: int) -> object:  # noqa: ARG001
            raise RuntimeError("sampling blew up")

        # 故障必须在 feed 线程读到事件**之前**注入：provider 由 start() 内部构造并立即起线程，
        # 若等 start() 返回后再赋值，11 条事件可能在赋值前被消费完（实测 2/20 flaky）。
        # 这里把注入点挂到 provider.start 之前，保证第一个事件就能观测到故障。
        real_start = provider_module.MarketFeedProvider.start

        def start_with_hostile(self: object) -> None:
            self.account_provider = hostile                  # type: ignore[attr-defined]
            real_start(self)                                 # type: ignore[arg-type]

        with mock.patch.object(provider_module.MarketFeedProvider, "start", start_with_hostile):
            runtime.start()
        self.run_until_done(runtime)
        try:
            stats = self.provider_stats(runtime)
            self.assertTrue(stats.get("completed"))
            self.assertGreater(runtime.service.market_history_view().counts["states"], 0)
            self.assertGreaterEqual(stats.get("account_sampling_errors", 0), 1)
        finally:
            runtime.stop()
        self.assertIs(runtime.service.run_registry_view().load(runtime.run_id).status, RunStatus.COMPLETED)

    def test_without_accounting_provider_semantics_unchanged(self) -> None:
        runtime = ProductRuntime(profile=self.feed_profile(RuntimeMode.REPLAY))
        runtime.start()
        self.run_until_done(runtime)
        try:
            self.assertIsNone(runtime.service.account_timeline_view())      # 未接线 ⇒ None（不伪造）
            self.assertGreater(runtime.service.market_history_view().counts["states"], 0)
        finally:
            runtime.stop()
