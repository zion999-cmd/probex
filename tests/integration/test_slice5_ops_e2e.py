"""Closure Slice 5 产品级 E2E（F-12 / F-15）。

A local trial（loopback，无 auth）
B non-loopback auth（未 opt-in 拒绝 / 缺 token 拒绝 / 401 / 200）
C retention（prune 生效、active 保留、未配置 UNBOUNDED）
D crash / restart（graceful COMPLETED；crash ⇒ INCOMPLETE；restart 历史/HWM/config 仍在）
E health split（process live = true 但 readiness 可 blocked、execution health 可 degraded）
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import AssemblyError, FeedProfile, ProductRuntime, RuntimeProfile
from runtime.observability import configure_logging, reset_logging
from storage.retention import RetentionPolicy
from storage.run_registry import JsonRunRegistry
from tests import scenarios
from tests.support import write_store

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _value(node: object) -> object:
    """解包 Fact（`{known,value,reason}`）取原始值；非 Fact 原样返回。"""
    if isinstance(node, dict) and "known" in node and "value" in node:
        return node["value"]
    return node


class Slice5RuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="probex-slice5-"))
        self.server = None
        self.thread = None
        self.runtime: ProductRuntime | None = None
        self.procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if self.runtime is not None:
            try:
                self.runtime.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        reset_logging()

    # ------------------------------------------------------------------ helpers

    def store(self, name: str = "events.jsonl") -> str:
        path = self.tmp / name
        if not path.exists():
            write_store(path, scenarios.reference_events())
        return str(path)

    def trial_entries(self) -> tuple[ConfigEntry, ...]:
        profile = json.loads((PROJECT_ROOT / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
        return tuple(ConfigEntry(name=str(name), source=ConfigSource.FILE, value=Fact.of(value))
                     for name, value in profile.items())

    def build_runtime(self, *, extra: tuple[ConfigEntry, ...] = (), host: str = "127.0.0.1",
                      allow_non_loopback: bool = False, auth_token_ref: str | None = None,
                      registry_dir: str | None = None, feed: bool = True,
                      mode: RuntimeMode = RuntimeMode.REPLAY) -> ProductRuntime:
        feed_profile = None if not feed else FeedProfile(
            event_store=self.store(), window_ms=600_000, bucket_ms=1_000, max_points=200,
            price_levels=5, history_capacity=500, view_depth=10)
        runtime = ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=self.trial_entries() + extra, mode=mode, host=host,
            allow_non_loopback=allow_non_loopback, auth_token_ref=auth_token_ref,
            run_registry_dir=registry_dir or str(self.tmp / "runs"), feed=feed_profile))
        self.runtime = runtime
        return runtime

    def start_with_http(self, runtime: ProductRuntime) -> str:
        runtime.start()
        provider = getattr(runtime, "_feed_provider", None)
        deadline = time.time() + 5
        while provider is not None and time.time() < deadline and not provider.stats.get("completed"):
            time.sleep(0.02)
        return self.serve_http(runtime)

    def serve_http(self, runtime: ProductRuntime) -> str:
        self.server = runtime.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return runtime.server_url()

    def call(self, base: str, path: str, token: str | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(f"{base}{path}")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with OPENER.open(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))


class A_LocalTrialTest(Slice5RuntimeTestCase):
    def test_loopback_runs_without_auth_and_exposes_ops_posture(self) -> None:
        log_stream = io.StringIO()
        configure_logging(level="INFO", stream=log_stream)
        runtime = self.build_runtime()
        base = self.start_with_http(runtime)
        status, live = self.call(base, "/health/live")
        self.assertEqual(status, 200)
        self.assertTrue(live["live"])
        runtime.stop()

        events = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
        names = [event["event"] for event in events]
        # 进程级 `startup`/`shutdown` 由 main() 产生（见 D 的真实启动子进程断言）
        for expected in ("run_start", "runtime_start", "run_finalize", "runtime_stop"):
            with self.subTest(event=expected):
                self.assertIn(expected, names)
        for event in events:
            self.assertEqual(event["component"] in ("runtime", "storage", "actions", "api"), True)

    def test_ops_endpoint_shows_network_logging_retention_and_health(self) -> None:
        configure_logging(level="INFO", stream=io.StringIO())
        runtime = self.build_runtime()
        base = self.start_with_http(runtime)
        status, payload = self.call(base, "/api/v1/ops")
        self.assertEqual(status, 200)
        ops = payload["ops"]
        network = _value(ops["network"])
        retention = _value(ops["retention"])
        logging_posture = _value(ops["logging"])
        self.assertTrue(network["loopback"])
        self.assertFalse(network["auth_required"])
        self.assertFalse(network["allow_non_loopback"])
        self.assertFalse(retention["bounded"])                          # 未配置 ⇒ UNBOUNDED
        self.assertIn("level", logging_posture)
        self.assertTrue(_value(ops["process_live"]))
        self.assertEqual(_value(ops["runtime_state"]), "RUNNING")
        # 四层彼此独立：liveness ≠ readiness
        self.assertNotEqual(_value(ops["trade_readiness"]), _value(ops["process_live"]))


class B_NonLoopbackAuthTest(Slice5RuntimeTestCase):
    def test_refuse_without_optin_and_without_token(self) -> None:
        entries = self.trial_entries()
        with self.assertRaises(AssemblyError):
            ProductRuntime(profile=RuntimeProfile(symbol="BTCUSDT", config_entries=entries,
                                                  host="0.0.0.0",
                                                  run_registry_dir=str(self.tmp / "r1")))
        with self.assertRaises(AssemblyError):
            ProductRuntime(profile=RuntimeProfile(symbol="BTCUSDT", config_entries=entries,
                                                  host="0.0.0.0", allow_non_loopback=True,
                                                  run_registry_dir=str(self.tmp / "r2")))
        # 引用了不存在的 secret ⇒ fail closed（不静默继续）
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROBEX_API_TOKEN", None)
            with self.assertRaises(AssemblyError):
                ProductRuntime(profile=RuntimeProfile(symbol="BTCUSDT", config_entries=entries,
                                                      host="0.0.0.0", allow_non_loopback=True,
                                                      auth_token_ref="env:PROBEX_API_TOKEN",
                                                      run_registry_dir=str(self.tmp / "r3")))

    def test_bearer_token_is_enforced_and_never_leaks(self) -> None:
        token = "slice5-super-secret-token"
        with mock.patch.dict(os.environ, {"PROBEX_API_TOKEN": token}, clear=False):
            runtime = self.build_runtime(host="0.0.0.0", allow_non_loopback=True,
                                         auth_token_ref="env:PROBEX_API_TOKEN")
            base = self.start_with_http(runtime)

            self.assertEqual(self.call(base, "/health/live")[0], 200)          # liveness 免认证
            self.assertEqual(self.call(base, "/api/v1/snapshot")[0], 401)      # 缺 token
            self.assertEqual(self.call(base, "/api/v1/snapshot", "wrong")[0], 401)
            self.assertEqual(self.call(base, "/api/v1/snapshot", token)[0], 200)
            self.assertEqual(self.call(base, "/api/v1/ops", token)[0], 200)
            self.assertEqual(self.call(base, "/api/v1/actions", token)[0], 200)  # actions 无匿名旁路
            self.assertEqual(self.call(base, "/api/v1/actions")[0], 401)

            from product.serialization import snapshot_to_jsonable

            snapshot_text = json.dumps(snapshot_to_jsonable(runtime.service.snapshot()))
            self.assertNotIn(token, snapshot_text)
            self.assertIn("env:PROBEX_API_TOKEN", snapshot_text)
            audit_text = json.dumps(runtime._gateway.audit.entries(), default=str)
            self.assertNotIn(token, audit_text)


class C_RetentionTest(Slice5RuntimeTestCase):
    def _seed(self, count: int) -> str:
        registry_dir = self.tmp / "runs-retention"
        registry = JsonRunRegistry(registry_dir)
        from product.types import RuntimeIdentity

        for index in range(count):
            runtime_id = f"rt-{index}"
            identity = RuntimeIdentity(mode=RuntimeMode.REPLAY, environment="local", venue="binance",
                                       symbol="BTCUSDT", runtime_id=runtime_id,
                                       started_at=1_000 + index, data_timestamp=Fact.unknown("x"))
            registry.start(runtime=identity, run_id=f"run-{index}", now_ms=1_000 + index)
            registry.finalize(run_id=f"run-{index}", ended_at=2_000 + index, summary={})
        return str(registry_dir)

    def test_prune_removes_only_old_finished_runs_and_keeps_active(self) -> None:
        registry_dir = self._seed(4)
        registry = JsonRunRegistry(registry_dir)
        registry.start(runtime=__import__("product.types", fromlist=["RuntimeIdentity"]).RuntimeIdentity(
            mode=RuntimeMode.REPLAY, environment="local", venue="binance", symbol="BTCUSDT",
            runtime_id="rt-active", started_at=9_000, data_timestamp=Fact.unknown("x")),
            run_id="run-active", now_ms=9_000)
        registry.mark_active(run_id="run-active", runtime_id="rt-active", mode="REPLAY",
                             symbol="BTCUSDT", now_ms=9_000)

        log_stream = io.StringIO()
        configure_logging(level="INFO", stream=log_stream)
        runtime = self.build_runtime(extra=(ConfigEntry(name="retention.run_max_runs",
                                                        source=ConfigSource.CLI, value=Fact.of(2)),),
                                     registry_dir=registry_dir, feed=False)
        report = runtime.run_retention(now_ms=10_000)
        self.assertTrue(report.applied)
        self.assertEqual(report.skipped_active_run_id, "run-active")
        self.assertNotIn("run-active", report.removed_run_ids)
        remaining = {record.run_id for record in registry.list()}
        self.assertIn("run-active", remaining)
        completed = {record.run_id for record in registry.finalized_runs()}
        self.assertEqual(completed, {"run-2", "run-3"})                # 保留最新 2 个
        self.assertIsNone(registry.load("run-0"))
        events = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
        prune_events = [event for event in events if event["event"] == "retention_prune"]
        self.assertEqual(len(prune_events), 1)
        self.assertEqual(prune_events[0]["reason_code"], "RETENTION_PRUNED")

    def test_unbounded_policy_is_visible_and_deletes_nothing(self) -> None:
        registry_dir = self._seed(3)
        runtime = self.build_runtime(registry_dir=registry_dir, feed=False)
        self.assertTrue(runtime.retention_policy.is_unbounded)
        report = runtime.run_retention(now_ms=10_000)
        self.assertFalse(report.applied)
        payload = runtime.ops_posture()
        self.assertFalse(payload["retention"]["bounded"])  # type: ignore[index]
        self.assertEqual(len(JsonRunRegistry(registry_dir).list()), 3)


class D_CrashRestartTest(Slice5RuntimeTestCase):
    def _spawn(self, registry_dir: str, event_store: str) -> tuple[subprocess.Popen, pathlib.Path]:
        env = {**os.environ, "PROBEX_RUN_REGISTRY_DIR": registry_dir}
        log_path = self.tmp / "subprocess.log"
        log_file = log_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "runtime.assembly", "--symbol", "BTCUSDT", "--mode", "replay",
             "--port", "0", "--run-registry-dir", registry_dir, "--event-store", event_store,
             "--config-file", str(PROJECT_ROOT / "profiles" / "trial-local.json")],
            cwd=str(PROJECT_ROOT), env=env, stdout=subprocess.PIPE, stderr=log_file, text=True)
        log_file.close()   # 子进程已持有 fd；父进程关闭自己的副本
        self.procs.append(proc)
        deadline = time.time() + 15
        while time.time() < deadline:
            registry = JsonRunRegistry(registry_dir)
            if registry.active_run_id() is not None:
                return proc, log_path
            if proc.poll() is not None:
                raise AssertionError(f"runtime exited early: {proc.returncode}")
            time.sleep(0.1)
        raise AssertionError("runtime did not become active in time")

    def test_graceful_stop_completes_and_clears_marker(self) -> None:
        registry_dir = str(self.tmp / "runs-graceful")
        runtime = self.build_runtime(registry_dir=registry_dir)
        runtime.start()
        runtime.stop()
        registry = JsonRunRegistry(registry_dir)
        self.assertIs(registry.load(runtime.run_id).status.value, "COMPLETED")
        self.assertIsNone(registry.active_run_id())
        self.assertFalse((pathlib.Path(registry_dir) / "active.json").exists())

    def test_crash_is_incomplete_and_restart_keeps_history_and_hwm(self) -> None:
        registry_dir = str(self.tmp / "runs-crash")
        event_store = self.store()
        proc, log_path = self._spawn(registry_dir, event_store)
        registry = JsonRunRegistry(registry_dir)
        running_id = registry.active_run_id()
        self.assertIsNotNone(running_id)
        # 真实启动路径产生了进程级 `startup` 结构化日志（stderr JSON）
        deadline = time.time() + 5
        while time.time() < deadline and "startup" not in log_path.read_text(encoding="utf-8"):
            time.sleep(0.05)
        self.assertIn("startup", log_path.read_text(encoding="utf-8"))

        proc.send_signal(signal.SIGKILL)          # crash-like（不做 graceful finalize）
        proc.wait(timeout=5)

        registry = JsonRunRegistry(registry_dir)
        # stale/dead marker 不得被当作 RUNNING
        self.assertIsNone(registry.active_run_id())
        record = registry.load(running_id)
        self.assertEqual(record.status.value, "INCOMPLETE")            # 不伪造 COMPLETED

        # HWM durable state（与 run registry 同域但独立文件）
        from risk.high_watermark import EquityHighWatermarkState, HighWatermarkScope, \
            HighWatermarkStatus
        from storage.high_watermark import JsonHighWatermarkStore

        hwm_path = pathlib.Path(registry_dir) / "equity_hwm.json"
        store = JsonHighWatermarkStore(hwm_path)
        state = EquityHighWatermarkState(
            scope=HighWatermarkScope.TESTNET, deployment_id="dep-1", activation_id="act-1",
            activation_ts=1_000, activation_equity=10_000.0, peak_equity=10_500.0, peak_ts=2_000,
            last_equity=10_400.0, last_observed_ts=3_000, capital_flow_checked_through=3_000,
            generation=1, status=HighWatermarkStatus.ACTIVE)
        store.save(state)

        # restart：历史仍在、HWM 仍在、config provenance 可重新读取
        runtime = self.build_runtime(registry_dir=registry_dir)
        runtime.start()
        history = {item.run_id: item.status.value for item in runtime.service.run_registry_view().list()}
        self.assertEqual(history[running_id], "INCOMPLETE")
        self.assertTrue(runtime.service.snapshot().config.fingerprint.known)
        reloaded = JsonHighWatermarkStore(hwm_path).load()
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.peak_equity, 10_500.0)
        self.assertEqual(reloaded.activation_id, "act-1")
        runtime.stop()


class E_HealthSplitTest(Slice5RuntimeTestCase):
    def test_process_live_while_readiness_blocked_and_execution_degraded(self) -> None:
        from types import SimpleNamespace

        runtime = self.build_runtime(feed=False)
        runtime.attach_readiness(lambda: SimpleNamespace(status="blocked",
                                                         reasons=("HISTORICAL_DRAWDOWN_UNKNOWN",)))
        base = self.serve_http(runtime)
        status, payload = self.call(base, "/api/v1/ops")
        self.assertEqual(status, 200)
        ops = payload["ops"]
        self.assertTrue(_value(ops["process_live"]))
        self.assertEqual(_value(ops["trade_readiness"]), "blocked")
        self.assertIn("HISTORICAL_DRAWDOWN_UNKNOWN", ops["trade_readiness_reasons"])
        self.assertEqual(_value(ops["runtime_state"]), "STARTING")  # 未 start ⇒ 与 ready 无关
        self.assertIsNotNone(_value(ops["operational_warning"]))
        # liveness 端点只回答"进程活着"
        _, live = self.call(base, "/health/live")
        self.assertEqual(live, {**live, "live": True})
        self.assertNotIn("trade_readiness", live)

        # F-12/F-15：Assistant 能解释 operational posture（只读，不新增写 action）
        request = urllib.request.Request(
            f"{base}/api/v1/actions/explain.entity", method="POST",
            data=json.dumps({"parameters": {"kind": "ops",
                                             "identity": "process_live_readiness_blocked"}}).encode(),
            headers={"Content-Type": "application/json"})
        with OPENER.open(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        self.assertEqual(body["action"]["status"], "SUCCEEDED")
        self.assertEqual(body["action"]["result"]["reason_code"],
                         "PROCESS_LIVE_READINESS_BLOCKED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
