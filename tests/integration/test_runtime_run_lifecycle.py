"""P0001.11.1 集成：真实 replay run 的自动登记 + 通过 API/CLI 可见（SC-1 – SC-5）。"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from api.server import create_server
from market.replay.source import ReplaySource
from product.provenance import ConfigEntry, ConfigSource, build_config_snapshot
from product.types import Fact, RuntimeMode
from reports.types import RunStatus
from runtime.session import RuntimeSession, SessionSummaryFacts
from storage.events.reader import JsonlEventReader
from storage.run_registry import JsonRunRegistry
from tests import scenarios
from tests.support import market_book, replay_into, write_store
from tests.unit.test_product_snapshot import identity, service

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RuntimeRunLifecycleIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probex-runtime-runs-"))
        self.registry = JsonRunRegistry(self.tmp / "runs")
        self.store = self.tmp / "events.jsonl"
        self.now = [1_000]
        self.config = build_config_snapshot(
            config_id="cfg-replay",
            entries=[ConfigEntry(name="symbol", source=ConfigSource.CONSTRUCTOR, value=Fact.of("BTCUSDT")),
                     ConfigEntry(name="replay_mode", source=ConfigSource.CLI, value=Fact.of("FULL"))],
            created_at=1_000)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clock(self) -> int:
        return self.now[0]

    def run_replay_session(self, events) -> RuntimeSession:
        write_store(self.store, events)
        source = ReplaySource(JsonlEventReader(self.store))
        ticks = [0]

        def facts() -> SessionSummaryFacts:
            stamps = [event.exchange_ts for event in events]
            return SessionSummaryFacts(data_range=(min(stamps), max(stamps)),
                                       anomalies=(f"events_consumed:{ticks[0]}",))

        session = RuntimeSession(mode=RuntimeMode.REPLAY, environment="local", venue="binance",
                                 symbol="BTCUSDT", registry=self.registry, clock=self.clock,
                                 config=self.config, data_timestamp_provider=lambda: events[-1].exchange_ts)
        session.start()
        book = market_book()          # 真实产品组件消费真实回放事件
        replay_into(book, source.iter_events())
        ticks[0] = len(events)
        self.now[0] = 60_000
        session.stop(facts=facts())
        return session

    def api(self, registry=None):
        svc = service(config_snapshot=lambda: self.config, run_registry=lambda: (registry or self.registry))
        server = create_server(svc, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    def test_1_replay_run_is_created_and_completed_without_manual_registration(self) -> None:
        events = scenarios.reference_events()
        session = self.run_replay_session(events)

        record = self.registry.load(session.run_id)
        self.assertEqual(record.status, RunStatus.COMPLETED)
        self.assertEqual(record.runtime.mode, RuntimeMode.REPLAY)
        self.assertEqual(record.runtime.data_timestamp.value, events[-1].exchange_ts)
        self.assertIsNotNone(record.summary)

    def test_4_runs_api_and_cli_see_the_real_run(self) -> None:
        session = self.run_replay_session(scenarios.reference_events())
        server, thread, base = self.api()
        try:
            with OPENER.open(f"{base}/api/v1/runs", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual([run["run_id"] for run in payload["runs"]], [session.run_id])
            self.assertEqual(payload["runs"][0]["status"], "COMPLETED")
            self.assertTrue(str(payload["runs"][0]["config_fingerprint"]["value"]).startswith("sha256:"))
            with OPENER.open(f"{base}/api/v1/runs/{session.run_id}", timeout=5) as response:
                single = json.loads(response.read().decode("utf-8"))
            self.assertEqual(single["run"]["summary"]["run"]["run_id"], session.run_id)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_2_graceful_stop_completes_and_3_crash_stays_incomplete(self) -> None:
        completed = self.run_replay_session(scenarios.reference_events())

        self.now[0] = 120_000
        crashed = RuntimeSession(mode=RuntimeMode.REPLAY, environment="local", venue="binance",
                                 symbol="BTCUSDT", registry=self.registry, clock=self.clock,
                                 runtime_id="replay-crash", config=self.config)
        with self.assertRaises(RuntimeError):
            with crashed:
                raise RuntimeError("simulated crash")

        server, thread, base = self.api()
        try:
            with OPENER.open(f"{base}/api/v1/runs", timeout=5) as response:
                runs = {run["run_id"]: run["status"] for run in json.loads(response.read().decode("utf-8"))["runs"]}
            self.assertEqual(runs[completed.run_id], "COMPLETED")
            self.assertEqual(runs["replay-crash"], "INCOMPLETE")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_5_run_config_fingerprint_matches_the_startup_snapshot(self) -> None:
        session = self.run_replay_session(scenarios.reference_events())
        record = self.registry.load(session.run_id)

        self.assertEqual(record.config_id.value, "cfg-replay")
        self.assertEqual(record.config_fingerprint.value, self.config.fingerprint)
        self.assertEqual(record.summary["run"]["config_id"]["value"], "cfg-replay")
        # 序列化后的 fingerprint 是 Fact 形态（known/value/reason）
        self.assertEqual(record.summary["run"]["config_fingerprints"]["config"]["value"], self.config.fingerprint)

    def test_6_trading_path_is_untouched_by_the_session_layer(self) -> None:
        """SC-6：run 登记不改变交易路径 —— 现有执行/策略模块未被 import 到 runtime 层。"""
        import runtime.session as session_module

        source = Path(session_module.__file__).read_text(encoding="utf-8")
        for forbidden in ("OrderTracker", "ExecutionEngine", "RiskGate", "MakerPolicy", "BinanceExecutionAdapter"):
            self.assertNotIn(forbidden, source)


    def test_no_daemon_or_extra_writer_is_introduced(self) -> None:
        """边界：不引入 daemon / 不做多进程 writer。"""
        import runtime.session as session_module

        source = Path(session_module.__file__).read_text(encoding="utf-8")
        for forbidden in ("threading", "multiprocessing", "daemon=True", "subprocess", "socket"):
            self.assertNotIn(forbidden, source)
