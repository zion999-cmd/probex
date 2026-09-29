"""P0001.11.1：Runtime Session 生命周期（SC-1 / SC-2 / SC-3 / SC-5 / SC-6）。"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest
from pathlib import Path

from product.provenance import ConfigEntry, ConfigSource, build_config_snapshot
from product.types import Fact, RuntimeMode
from reports.metrics import EquitySample, RealizedTradeResult
from reports.types import RunStatus
from runtime.session import RuntimeSession, RuntimeSessionError, SessionSummaryFacts
from storage.run_registry import JsonRunRegistry

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


def config(config_id: str = "cfg-1", *, value: str = "BTCUSDT", created_at: int = 1_000):
    return build_config_snapshot(config_id=config_id,
                                 entries=[ConfigEntry(name="symbol", source=ConfigSource.CONSTRUCTOR,
                                                      value=Fact.of(value))], created_at=created_at)


class RuntimeSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="probex-session-"))
        self.registry = JsonRunRegistry(self.root)
        self.now = [1_000]

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def clock(self) -> int:
        return self.now[0]

    def session(self, **overrides: object) -> RuntimeSession:
        values: dict[str, object] = {
            "mode": RuntimeMode.TESTNET, "environment": "testnet", "venue": "binance",
            "symbol": "BTCUSDT", "registry": self.registry, "clock": self.clock, "config": config(),
        }
        values.update(overrides)
        return RuntimeSession(**values)  # type: ignore[arg-type]

    def test_1_starting_a_runtime_creates_a_run_record(self) -> None:
        session = self.session()
        record = session.start()

        self.assertEqual(record.status, RunStatus.RUNNING)
        self.assertEqual(record.run_id, "testnet-1000")
        self.assertEqual(record.runtime.mode, RuntimeMode.TESTNET)
        self.assertTrue(session.active)
        # registry 的读侧语义 = "created but no finalization"（下一次检查视为 INCOMPLETE）；
        # 会话自己在运行期间知道自己是 RUNNING（不依赖存储判断活性）
        self.assertEqual(self.registry.load("testnet-1000").status, RunStatus.INCOMPLETE)

    def test_2_graceful_stop_finalizes_completed_with_summary(self) -> None:
        session = self.session(mode=RuntimeMode.PAPER)
        session.start()
        self.now[0] = 60_000
        facts = SessionSummaryFacts(
            fees=0.02, realized_pnl=1.5, unrealized_pnl=-0.25, final_position=0.0,
            max_confirmed_exposure=58.8, max_total_exposure=58.8,
            equity_samples=tuple(EquitySample(ts=i * 60_000, equity=1_000.0 + i * 0.5) for i in range(40)),
            realized_trades=(RealizedTradeResult(2.0), RealizedTradeResult(-1.0)),
        )
        record = session.stop(facts=facts)

        self.assertEqual(record.status, RunStatus.COMPLETED)
        self.assertEqual(record.ended_at.value, 60_000)
        self.assertIsNotNone(record.summary)
        metrics = record.summary["metrics"]
        self.assertTrue(metrics["profit_factor"]["known"])
        self.assertTrue(metrics["sharpe"]["known"])
        self.assertTrue(metrics["run_mdd"]["known"])
        self.assertEqual(record.summary["run"]["config_id"]["value"], "cfg-1")
        self.assertFalse(session.active)

    def test_3_exception_leaves_the_run_incomplete_and_reraises(self) -> None:
        session = self.session(mode=RuntimeMode.REPLAY)

        with self.assertRaises(RuntimeError):
            with session:
                raise RuntimeError("boom")

        record = self.registry.load(session.run_id)
        self.assertEqual(record.status, RunStatus.INCOMPLETE)
        self.assertIsNone(record.summary)

    def test_4_four_modes_share_one_semantics(self) -> None:
        for mode in RuntimeMode:
            with self.subTest(mode=mode.value):
                now = [2_000]
                session = RuntimeSession(mode=mode, environment="local", venue="binance", symbol="BTCUSDT",
                                         registry=self.registry, clock=lambda: now[0], config=config())
                session.start()
                now[0] = 3_000
                record = session.stop(facts=SessionSummaryFacts(final_position=0.0))
                self.assertEqual(record.runtime.mode, mode)
                self.assertEqual(record.status, RunStatus.COMPLETED)

    def test_5_config_fingerprint_is_bound_at_start(self) -> None:
        session = self.session()
        record = session.start()

        self.assertEqual(record.config_fingerprint.value, config().fingerprint)
        self.assertEqual(record.config_id.value, "cfg-1")

    def test_5b_stopping_with_a_different_config_is_refused(self) -> None:
        session = self.session()
        session.start()
        session.config = config("cfg-2", value="ETHUSDT")  # 模拟运行中 config 快照被替换（内容不同）

        with self.assertRaises(RuntimeSessionError):
            session.stop(facts=SessionSummaryFacts())

        # 拒绝 finalize ⇒ 存储里仍然只有 start（读侧 = created but no finalization）
        self.assertEqual(self.registry.load(session.run_id).status, RunStatus.INCOMPLETE)
        self.assertEqual(session.record.status, RunStatus.RUNNING)

    def test_run_id_cannot_be_reused(self) -> None:
        self.session().start()

        with self.assertRaises(RuntimeSessionError):
            self.session(runtime_id="testnet-1000").start()

    def test_session_cannot_start_twice_or_stop_twice(self) -> None:
        session = self.session()
        session.start()
        with self.assertRaises(RuntimeSessionError):
            session.start()
        session.stop(facts=SessionSummaryFacts())
        with self.assertRaises(RuntimeSessionError):
            session.stop(facts=SessionSummaryFacts())

    def test_stop_before_start_is_refused(self) -> None:
        with self.assertRaises(RuntimeSessionError):
            self.session().stop(facts=SessionSummaryFacts())

    def test_data_timestamp_and_range_are_recorded_when_known(self) -> None:
        session = self.session(data_timestamp_provider=lambda: 5_555,
                               data_range=Fact.of([100, 5_555]))
        record = session.start()

        self.assertEqual(record.runtime.data_timestamp.value, 5_555)
        self.assertEqual(record.data_range.value, [100, 5_555])

    def test_unknown_data_timestamp_stays_unknown(self) -> None:
        record = self.session().start()
        self.assertFalse(record.runtime.data_timestamp.known)

    def test_6_session_layer_has_no_trading_capability(self) -> None:
        """SC-6：不改变交易路径 —— 本层不得 import strategy / risk / execution / connectors。"""
        source = (PROJECT_ROOT / "runtime" / "session.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            for root in ("strategy", "risk", "execution", "connectors", "live", "market.book"):
                with self.subTest(import_line=stripped):
                    self.assertFalse(stripped.startswith(f"from {root}.") or stripped.startswith(f"import {root}"))
        for forbidden in ("submit(", "cancel(", "place_", "kill_switch", "RiskGate"):
            self.assertNotIn(forbidden, source)
