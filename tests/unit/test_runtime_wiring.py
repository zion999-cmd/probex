"""P0001.11.2：RuntimeSession 生产接线（生命周期监听；stop 失败绝不 COMPLETED）。"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest
from pathlib import Path

from product.provenance import ConfigEntry, ConfigSource, build_config_snapshot
from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports.types import RunStatus
from runtime.session import RuntimeSession, SessionSummaryFacts
from runtime.wiring import (
    SessionHost,
    WiringError,
    open_live_session,
    open_paper_session,
    open_replay_session,
    open_testnet_session,
)
from storage.run_registry import JsonRunRegistry

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


def config():
    return build_config_snapshot(config_id="cfg-wire",
                                 entries=[ConfigEntry(name="symbol", source=ConfigSource.CONSTRUCTOR,
                                                      value=Fact.of("BTCUSDT"))], created_at=1_000)


class SessionHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="probex-wiring-"))
        self.registry = JsonRunRegistry(self.root)
        self.now = [1_000]
        self.calls: list[str] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def clock(self) -> int:
        return self.now[0]

    def session(self, mode: RuntimeMode = RuntimeMode.REPLAY, **overrides: object) -> RuntimeSession:
        values: dict[str, object] = {"mode": mode, "environment": "local", "venue": "binance",
                                     "symbol": "BTCUSDT", "registry": self.registry, "clock": self.clock,
                                     "config": config()}
        values.update(overrides)
        return RuntimeSession(**values)  # type: ignore[arg-type]

    def test_graceful_finish_registers_completed(self) -> None:
        host = SessionHost(session=self.session(), stop_hook=lambda: self.calls.append("stop_hook"))
        host.start()
        self.now[0] = 5_000
        record = host.finish(facts=SessionSummaryFacts(final_position=0.0))

        self.assertEqual(record.status, RunStatus.COMPLETED)
        self.assertEqual(self.calls, ["stop_hook"])
        self.assertEqual(self.registry.load(host.run_id).status, RunStatus.COMPLETED)

    def test_stop_hook_runs_before_finalization(self) -> None:
        session = self.session()

        def stop_hook() -> None:
            self.calls.append("stop_hook")
            # 原 Owner 的 stop 执行时：run **还没有** finalization，且本进程仍是 active 拥有者
            # ⇒ 读侧按 F-10 语义为 RUNNING（不是 INCOMPLETE，也不是 COMPLETED）
            self.assertEqual(self.registry.load(session.run_id).status, RunStatus.RUNNING)
            self.assertEqual(self.registry.active_run_id(), session.run_id)

        host = SessionHost(session=session, stop_hook=stop_hook)
        host.start()
        host.finish(facts=SessionSummaryFacts())
        self.assertEqual(self.calls, ["stop_hook"])

    def test_stop_hook_failure_finalizes_incomplete_and_reraises(self) -> None:
        session = self.session()

        def failing_stop() -> None:
            self.calls.append("stop_hook")
            raise RuntimeError("engine refused to stop cleanly")

        host = SessionHost(session=session, stop_hook=failing_stop)
        host.start()
        with self.assertRaises(RuntimeError):
            host.finish(facts=SessionSummaryFacts())

        record = self.registry.load(host.run_id)
        self.assertEqual(record.status, RunStatus.INCOMPLETE)
        self.assertNotEqual(record.status, RunStatus.COMPLETED)
        self.assertEqual(self.calls, ["stop_hook"])

    def test_terminate_records_incomplete_with_reason(self) -> None:
        host = SessionHost(session=self.session(), facts_provider=lambda: SessionSummaryFacts(fees=0.01))
        host.start()
        record = host.terminate("manual abort for maintenance")

        self.assertEqual(record.status, RunStatus.INCOMPLETE)
        self.assertIsNotNone(record.summary)
        self.assertIn("manual abort for maintenance", record.summary["anomalies"])

    def test_context_manager_abnormal_exit_is_incomplete_and_reraises(self) -> None:
        host = SessionHost(session=self.session())
        with self.assertRaises(ValueError):
            with host:
                raise ValueError("boom")

        self.assertEqual(self.registry.load(host.run_id).status, RunStatus.INCOMPLETE)

    def test_context_manager_exception_still_attempts_the_original_stop(self) -> None:
        def failing_stop() -> None:
            self.calls.append("stop_hook")
            raise RuntimeError("stop failed")

        host = SessionHost(session=self.session(), stop_hook=failing_stop)
        with self.assertRaises(ValueError):
            with host:
                raise ValueError("original failure wins")

        self.assertEqual(self.calls, ["stop_hook"])
        self.assertEqual(self.registry.load(host.run_id).status, RunStatus.INCOMPLETE)

    def test_run_feed_counts_steps_and_calls_step_in_order(self) -> None:
        host = SessionHost(session=self.session())
        host.start()
        seen: list[int] = []
        self.now[0] = 2_000

        count = host.run_feed([1, 2, 3], step=seen.append)

        self.assertEqual(count, 3)
        self.assertEqual(host.steps, 3)
        self.assertEqual(seen, [1, 2, 3])
        host.finish(facts=SessionSummaryFacts())

    def test_run_feed_requires_started_session(self) -> None:
        host = SessionHost(session=self.session())
        with self.assertRaises(WiringError):
            host.run_feed([])

    def test_finish_twice_is_refused(self) -> None:
        host = SessionHost(session=self.session())
        host.start()
        host.finish(facts=SessionSummaryFacts())
        with self.assertRaises(WiringError):
            host.finish(facts=SessionSummaryFacts())

    def test_four_openers_share_one_lifecycle(self) -> None:
        openers = {RuntimeMode.REPLAY: open_replay_session, RuntimeMode.PAPER: open_paper_session,
                   RuntimeMode.TESTNET: open_testnet_session, RuntimeMode.LIVE: open_live_session}
        for mode, opener in openers.items():
            with self.subTest(mode=mode.value):
                now = [3_000 + list(openers).index(mode)]
                session = RuntimeSession(mode=mode, environment="local", venue="binance", symbol="BTCUSDT",
                                         registry=self.registry, clock=lambda now=now: now[0], config=config())
                host = opener(mode=mode, environment="local", venue="binance", symbol="BTCUSDT",
                              session=session)
                host.start()
                now[0] += 1_000
                record = host.finish(facts=SessionSummaryFacts(final_position=0.0))
                self.assertEqual(record.status, RunStatus.COMPLETED)
                self.assertIs(record.runtime.mode, mode)

    def test_openers_refuse_mode_or_identity_mismatch(self) -> None:
        session = self.session(mode=RuntimeMode.PAPER)
        with self.assertRaises(WiringError):
            open_replay_session(mode=RuntimeMode.REPLAY, environment="local", venue="binance",
                                symbol="BTCUSDT", session=session)
        with self.assertRaises(WiringError):
            open_paper_session(mode=RuntimeMode.PAPER, environment="testnet", venue="binance",
                               symbol="BTCUSDT", session=session)

    def test_wiring_layer_has_no_trading_capability_or_concurrency(self) -> None:
        source = (PROJECT_ROOT / "runtime" / "wiring.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("from ") or stripped.startswith("import "):
                for root in ("strategy", "risk", "execution", "connectors", "live", "market.book"):
                    with self.subTest(import_line=stripped):
                        self.assertFalse(stripped.startswith(f"from {root}.")
                                         or stripped.startswith(f"import {root}"))
        for forbidden in ("threading", "multiprocessing", "daemon=True", "submit(", "cancel(", "RiskGate"):
            self.assertNotIn(forbidden, source)


class ActiveMarkerSemanticsTest(unittest.TestCase):
    """F-10：active + 无 finalization ⇒ RUNNING；无 active marker ⇒ INCOMPLETE；finalize 后 ⇒ COMPLETED。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="probex-active-"))
        self.registry = JsonRunRegistry(self.root)
        self.session = RuntimeSession(mode=RuntimeMode.REPLAY, environment="local", venue="binance",
                                      symbol="BTCUSDT", registry=self.registry, clock=lambda: 1_000,
                                      runtime_id="rt-active")
        self.host = SessionHost(session=self.session)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_active_run_reads_as_running_not_incomplete(self) -> None:
        self.host.start()

        self.assertEqual(self.registry.active_run_id(), "rt-active")
        self.assertEqual(self.registry.load("rt-active").status, RunStatus.RUNNING)
        self.assertNotEqual(self.registry.load("rt-active").status, RunStatus.INCOMPLETE)

    def test_marker_is_cleared_after_graceful_stop(self) -> None:
        self.host.start()
        record = self.host.finish(facts=SessionSummaryFacts())

        self.assertIsNone(self.registry.active_run_id())
        self.assertEqual(record.status, RunStatus.COMPLETED)
        self.assertEqual(self.registry.load("rt-active").status, RunStatus.COMPLETED)

    def test_marker_is_cleared_on_abnormal_termination_and_run_is_incomplete(self) -> None:
        self.host.start()
        self.host.terminate("simulated crash")

        self.assertIsNone(self.registry.active_run_id())
        self.assertEqual(self.registry.load("rt-active").status, RunStatus.INCOMPLETE)

    def test_marker_with_a_dead_process_is_not_treated_as_active(self) -> None:
        """进程身份优先：pid 不存在 ⇒ 不是 RUNNING（也不需要 TTL 才能判定）。"""
        self.registry.start(runtime=RuntimeIdentity(mode=RuntimeMode.REPLAY, environment="local",
                                                   venue="binance", symbol="BTCUSDT",
                                                   runtime_id="rt-dead", started_at=1_000,
                                                   data_timestamp=Fact.unknown("n/a")),
                            run_id="rt-dead", now_ms=1_000)
        self.registry.mark_active(run_id="rt-dead", runtime_id="rt-dead", mode="REPLAY",
                                  symbol="BTCUSDT", now_ms=1_000, pid=999_999_99)

        self.assertIsNone(self.registry.active_run_id())
        self.assertEqual(self.registry.load("rt-dead").status, RunStatus.INCOMPLETE)

    def test_marker_only_clears_for_its_own_run(self) -> None:
        self.host.start()
        self.registry.clear_active("someone-else")

        self.assertEqual(self.registry.active_run_id(), "rt-active")
        self.host.finish(facts=SessionSummaryFacts())
