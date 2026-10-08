"""Closure Slice 4 产品级 E2E（F-08 / F-09 / F-13 / F-16 / F-19）。

真实启动 `runtime.assembly.ProductRuntime`（非 test harness 手工拼 service）+ 真实 HTTP：
A 因果 trace / B reason UX / C config provenance / D navigation / E runs pagination。
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile, build_profile_from_args
from storage.run_registry import JsonRunRegistry
from tests import scenarios
from tests.support import write_store

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Slice4RuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="probex-slice4-"))
        self.server = None
        self.thread = None
        self.runtime: ProductRuntime | None = None

    def tearDown(self) -> None:
        if self.runtime is not None:
            try:
                self.runtime.stop()
            except Exception:  # noqa: BLE001 - 幂等清理
                pass
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    # ------------------------------------------------------------------ helpers

    def store(self) -> str:
        path = self.tmp / "events.jsonl"
        if not path.exists():
            write_store(path, scenarios.reference_events())
        return str(path)

    def trial_entries(self) -> tuple[ConfigEntry, ...]:
        profile = json.loads((PROJECT_ROOT / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
        return tuple(ConfigEntry(name=str(name), source=ConfigSource.FILE, value=Fact.of(value))
                     for name, value in profile.items())

    def build_runtime(self, *, mode: RuntimeMode = RuntimeMode.PAPER,
                      extra: tuple[ConfigEntry, ...] = (),
                      registry_dir: str | None = None) -> ProductRuntime:
        feed = None if mode in (RuntimeMode.TESTNET, RuntimeMode.LIVE) else FeedProfile(
            event_store=self.store(), window_ms=600_000, bucket_ms=1_000, max_points=200,
            price_levels=5, history_capacity=500, view_depth=10)
        runtime = ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=self.trial_entries() + extra, mode=mode,
            run_registry_dir=registry_dir or str(self.tmp / f"runs-{mode.value.lower()}"), feed=feed))
        self.runtime = runtime
        return runtime

    def start_with_http(self, runtime: ProductRuntime) -> str:
        runtime.start()
        self._wait_for_feed(runtime)
        return self.serve_http(runtime)

    def serve_http(self, runtime: ProductRuntime) -> str:
        """只起 HTTP server（不建立新 run）——用于 pagination 等只读端点。"""
        self.server = runtime.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return runtime.server_url()

    def _wait_for_feed(self, runtime: ProductRuntime) -> None:
        provider = getattr(runtime, "_feed_provider", None)
        deadline = time.time() + 5
        while provider is not None and time.time() < deadline and not provider.stats.get("completed"):
            time.sleep(0.02)

    def get(self, base: str, path: str) -> tuple[int, dict]:
        with OPENER.open(f"{base}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def post(self, base: str, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(f"{base}{path}", method="POST",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with OPENER.open(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def invoke_with_confirmation(self, base: str, action_id: str, parameters: dict) -> dict:
        status, payload = self.post(base, f"/api/v1/actions/{action_id}", {"parameters": parameters})
        if status == 200:
            return payload["action"]
        self.assertEqual(status, 409, payload)
        confirmation = payload["action"]["confirmation_id"]
        status, payload = self.post(base, f"/api/v1/actions/{action_id}",
                                    {"parameters": parameters, "confirmation": confirmation})
        self.assertEqual(status, 200, payload)
        return payload["action"]


class A_CausalTraceTest(Slice4RuntimeTestCase):
    """A：真实 PAPER 路径产生完整因果链，并在 Activity（/evidence）按时间可见。"""

    def test_complete_time_ordered_trace_from_a_real_paper_run(self) -> None:
        runtime = self.build_runtime()
        base = self.start_with_http(runtime)
        runtime._accounting.update_mark_price("BTCUSDT", 100.0, timestamp=int(runtime.profile.clock()))
        # P0001.17：本地模拟器按真实 venue 规则拒绝"会被成交的 post-only" ⇒ smoke 单挂在盘口**下方**
        smoke = runtime.run_paper_smoke_order(price=90.0, quantity=0.001)
        self.assertTrue(smoke["submitted"], smoke)
        action = self.invoke_with_confirmation(base, "runtime.request_reconciliation", {})
        self.assertEqual(action["status"], "SUCCEEDED", action)

        _, payload = self.get(base, "/api/v1/evidence")
        trace = payload["evidence"]["trace"]
        stages = [entry["stage"] for entry in trace]
        for stage in ("market_state", "risk", "normalization", "order", "ack", "execution_event",
                      "cancel", "reconciliation"):
            with self.subTest(stage=stage):
                self.assertIn(stage, stages)

        # F-08：每条 entry 都带 canonical identity_kind；ts 已定时序单调
        for entry in trace:
            self.assertIn("identity_kind", entry)
            self.assertIn("latency_ms", entry)
        timed = [entry["ts"]["value"] for entry in trace if entry["ts"]["known"]]
        self.assertEqual(timed, sorted(timed))

        # RiskDecision 进入 trace，且归一到 client_order_id
        risk = next(entry for entry in trace if entry["stage"] == "risk")
        self.assertEqual(risk["outcome"], "allow")
        self.assertEqual(risk["identity_kind"], "client_order_id")
        self.assertEqual(risk["identity"]["value"], smoke["client_order_id"])
        # normalization evidence：输入 -> 归一值 + rounding
        normalization = next(entry for entry in trace if entry["stage"] == "normalization")
        self.assertIn("->", normalization["detail"])
        self.assertIn("ROUND_DOWN", normalization["detail"])
        # ack latency 来自真实 observer：smoke 单必须带真实样本；其它 ack 若缺样本必须带 reason
        ack = [entry for entry in trace if entry["stage"] == "ack"]
        self.assertTrue(ack)
        # 注：ack→order 的身份映射是既有 trace 的启发式（按时间就近匹配）。本地闭环同时存在
        # 策略订单与 smoke 单，且事件级模拟会让订单很快终态 ⇒ 不要求特定订单一定出现在 ack 段，
        # 但**必须有真实样本**，且缺样本的 entry 必须带 reason（不允许静默 UNKNOWN）。
        self.assertTrue(any(entry["latency_ms"]["known"] for entry in ack),
                        "no ack latency sample at all")
        for entry in ack:
            if not entry["latency_ms"]["known"]:
                self.assertTrue(entry["latency_ms"]["reason"], "unknown latency must carry a reason")
        # reconciliation 在同一 trace
        self.assertEqual(next(entry for entry in trace
                              if entry["stage"] == "reconciliation")["reason_code"]["value"],
                         "RECONCILIATION_REQUIRED")

        # 缺阶段的诚实表达
        prediction = next(entry for entry in trace if entry["stage"] == "prediction")
        self.assertEqual(prediction["outcome"], "absent")
        self.assertFalse(prediction["reason_code"]["known"])


class B_ReasonUxTest(Slice4RuntimeTestCase):
    """B：原始 code + human explanation（catalog），未知 code 不猜语义。"""

    def test_catalog_endpoint_and_snapshot_annotations(self) -> None:
        runtime = self.build_runtime()
        base = self.start_with_http(runtime)

        _, catalog = self.get(base, "/api/v1/reasons")
        codes = {item["reason_code"] for item in catalog["catalog"]}
        self.assertIn("HISTORICAL_DRAWDOWN_UNKNOWN", codes)
        self.assertIn("RATE_LIMIT_EXHAUSTED", codes)
        self.assertIn("RECONCILIATION_REQUIRED", codes)

        _, known = self.get(base, "/api/v1/reasons/HISTORICAL_DRAWDOWN_UNKNOWN")
        self.assertTrue(known["explanation"]["catalogued"])
        self.assertTrue(known["explanation"]["title"])
        self.assertTrue(known["explanation"]["suggested_next_step"])

        _, unknown = self.get(base, "/api/v1/reasons/FUTURE_UNKNOWN_CODE")
        self.assertEqual(unknown["explanation"]["reason_code"], "FUTURE_UNKNOWN_CODE")
        self.assertFalse(unknown["explanation"]["catalogued"])
        self.assertEqual(unknown["explanation"]["title"], "暂无解释")

        _, snapshot = self.get(base, "/api/v1/snapshot")
        self.assertTrue(snapshot["blockers"])
        for blocker in snapshot["blockers"]:
            self.assertIn("explanation", blocker)
            self.assertTrue(blocker["explanation"]["title"])

    def test_assistant_explain_reuses_the_same_catalog(self) -> None:
        runtime = self.build_runtime()
        base = self.start_with_http(runtime)
        status, payload = self.post(base, "/api/v1/actions/explain.entity",
                                    {"parameters": {"kind": "snapshot", "identity": "x"}})
        self.assertEqual(status, 200, payload)
        self.assertIn("blocker_explanations", payload["action"]["result"])


class C_ConfigProvenanceTest(Slice4RuntimeTestCase):
    """C：FILE + ENV + CLI 同键不同值 ⇒ CLI > ENV > FILE > CONSTRUCTOR；secret 只显示引用。"""

    def test_real_startup_path_resolves_precedence_and_redacts_secrets(self) -> None:
        config_file = self.tmp / "file.json"
        config_file.write_text(json.dumps({
            "projection.window_ms": 600_000, "projection.bucket_ms": 1_000,
            "projection.max_points": 200, "projection.price_levels": 5,
            "projection.history_capacity": 500, "projection.view_depth": 10,
            "venue.note": "from-file",
        }), encoding="utf-8")
        env = {
            "PROBEX_CONFIG_PROJECTION__MAX_POINTS": "150",     # ENV 覆盖 FILE
            "PROBEX_CONFIG_VENUE__NOTE": "from-env",           # ENV 覆盖 FILE
            "PROBEX_CONFIG_ONLY__ENV": "env-only",             # 只有 ENV
            "PROBEX_SECRET_BINANCE__API_KEY": "BINANCE_API_KEY",
        }
        argv = ["--symbol", "BTCUSDT", "--mode", "paper", "--event-store", self.store(),
                "--config-file", str(config_file),
                "--config", "projection.max_points=100"]        # CLI 覆盖 ENV
        with mock.patch.dict(os.environ, env, clear=False):
            profile = build_profile_from_args(argv)
            runtime = ProductRuntime(profile=profile)
            self.runtime = runtime
            snapshot = runtime.service.snapshot()

        entries = {entry.name: entry for entry in snapshot.config.entries}
        self.assertEqual(entries["projection.max_points"].source, "CLI")
        self.assertEqual(str(entries["projection.max_points"].value.value), "100")
        self.assertEqual(entries["venue.note"].source, "ENV")
        self.assertEqual(entries["only.env"].source, "ENV")
        self.assertTrue(snapshot.config.fingerprint.known)
        self.assertIn("sha256:", snapshot.config.fingerprint.value)

        secret = entries["binance.api_key"]
        self.assertEqual(secret.secret_ref, "env:BINANCE_API_KEY")
        self.assertFalse(secret.value.known)                   # 值从不记录
        self.assertIn("env:BINANCE_API_KEY", snapshot.config.secret_refs)
        # fingerprint 不含 secret 值
        self.assertNotIn("BINANCE_API_KEY=", str(snapshot.config.fingerprint.value))


class D_NavigationTest(Slice4RuntimeTestCase):
    """D：Assistant navigate/select 与 UI 共用同一 navigation contract。"""

    def test_navigate_and_select_use_canonical_targets(self) -> None:
        runtime = self.build_runtime()
        base = self.start_with_http(runtime)

        status, payload = self.post(base, "/api/v1/actions/navigate.surface",
                                    {"parameters": {"surface": "activity", "detail": "evidence",
                                                    "identity": "probex-1"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"]["result"]["target"], "#/activity/evidence/probex-1")

        status, payload = self.post(base, "/api/v1/actions/select.entity",
                                    {"parameters": {"kind": "order", "identity": "probex-7"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"]["result"]["navigation"]["target"],
                         "#/activity/evidence/probex-7")

        status, payload = self.post(base, "/api/v1/actions/select.entity",
                                    {"parameters": {"kind": "run", "identity": "run-3"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"]["result"]["navigation"]["target"],
                         "#/performance/runs/run-3")

        status, payload = self.post(base, "/api/v1/actions/navigate.surface",
                                    {"parameters": {"surface": "system", "detail": "readiness"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"]["result"]["target"], "#/system/readiness")

        # Run → Activity 携带 canonical run id（与 UI 链接同一 contract）
        status, payload = self.post(base, "/api/v1/actions/navigate.surface",
                                    {"parameters": {"surface": "activity", "detail": "run",
                                                    "identity": "run-3"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"]["result"]["target"], "#/activity/run/run-3")

        status, _ = self.post(base, "/api/v1/actions/navigate.surface",
                              {"parameters": {"surface": "not-a-surface"}})
        self.assertEqual(status, 502)                          # handler 拒绝（不猜 surface）


class E_RunsPaginationTest(Slice4RuntimeTestCase):
    """E：/api/v1/runs 有界分页，继续读取无重复/遗漏。"""

    def _seed_runs(self, count: int) -> str:
        registry_dir = self.tmp / "runs-page"
        registry = JsonRunRegistry(registry_dir)
        from product.types import RuntimeIdentity

        for index in range(count):
            identity = RuntimeIdentity(mode=RuntimeMode.PAPER, environment="local", venue="binance",
                                       symbol="BTCUSDT", runtime_id=f"rt-{index}",
                                       started_at=1_000 + index, data_timestamp=Fact.unknown("x"))
            registry.start(runtime=identity, run_id=f"run-{index}", now_ms=1_000 + index)
            registry.finalize(run_id=f"run-{index}", ended_at=2_000 + index, summary={})
        return str(registry_dir)

    def test_bounded_pagination_has_no_duplicates_or_gaps(self) -> None:
        registry_dir = self._seed_runs(5)
        runtime = self.build_runtime(registry_dir=registry_dir)
        base = self.serve_http(runtime)                     # 不 start ⇒ 不会新增第 6 个 run

        status, first = self.get(base, "/api/v1/runs?limit=2&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(first["runs"]), 2)
        self.assertEqual(first["pagination"]["total"], 5)
        self.assertTrue(first["pagination"]["has_more"])
        self.assertEqual(first["pagination"]["next_offset"], 2)

        seen: list[str] = []
        offset = 0
        while True:
            _, page = self.get(base, f"/api/v1/runs?limit=2&offset={offset}")
            seen.extend(run["run_id"] for run in page["runs"])
            if not page["pagination"]["has_more"]:
                break
            offset = page["pagination"]["next_offset"]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {f"run-{index}" for index in range(5)})

    def test_limit_above_the_safe_maximum_is_refused(self) -> None:
        registry_dir = self._seed_runs(2)
        runtime = self.build_runtime(registry_dir=registry_dir)
        base = self.serve_http(runtime)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            OPENER.open(f"{base}/api/v1/runs?limit=100000", timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_default_list_is_bounded_even_without_query(self) -> None:
        registry_dir = self._seed_runs(3)
        runtime = self.build_runtime(registry_dir=registry_dir)
        base = self.serve_http(runtime)
        _, payload = self.get(base, "/api/v1/runs")
        self.assertIn("pagination", payload)
        self.assertLessEqual(payload["pagination"]["limit"], payload["pagination"]["total"] + 1000)
        self.assertTrue(payload["pagination"]["limit"] > 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
