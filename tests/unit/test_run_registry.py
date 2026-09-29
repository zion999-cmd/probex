"""P0001.11 §2 / 裁决 B：Run Registry（append-only、INCOMPLETE、compare、无 retention）。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from product.provenance import ConfigSource, ConfigEntry, build_config_snapshot
from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports.types import RunStatus
from storage.run_registry import INDEX_FILE, RUNS_DIR, JsonRunRegistry, RunRegistryError


def runtime(runtime_id: str = "rt-1", mode: RuntimeMode = RuntimeMode.PAPER) -> RuntimeIdentity:
    return RuntimeIdentity(mode=mode, environment="testnet", venue="binance", symbol="BTCUSDT",
                           runtime_id=runtime_id, started_at=1_000, data_timestamp=Fact.of(2_000))


def config():
    return build_config_snapshot(config_id="cfg-1",
                                 entries=[ConfigEntry(name="symbol", source=ConfigSource.CONSTRUCTOR,
                                                      value=Fact.of("BTCUSDT"))],
                                 created_at=1_000)


def summary_payload(net_pnl: float | None, *, run_id: str) -> dict:
    metrics = {
        "net_pnl": ({"known": False, "value": None, "reason": "authoritative accounting fact not provided"}
                    if net_pnl is None else {"known": True, "value": net_pnl, "reason": None}),
        "fees": {"known": True, "value": 0.02, "reason": None},
    }
    return {"run": {"run_id": run_id}, "metrics": metrics}


class RunRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="probex-runs-"))
        self.registry = JsonRunRegistry(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_start_records_identity_and_config(self) -> None:
        record = self.registry.start(runtime=runtime(), run_id="run-1", now_ms=1_000, config=config())

        self.assertEqual(record.status, RunStatus.RUNNING)
        self.assertEqual(record.config_id.value, "cfg-1")
        self.assertTrue(str(record.config_fingerprint.value).startswith("sha256:"))
        self.assertFalse(record.ended_at.known)

    def test_finalize_appends_and_writes_an_atomic_record_file(self) -> None:
        self.registry.start(runtime=runtime(), run_id="run-1", now_ms=1_000, config=config())
        finalized = self.registry.finalize(run_id="run-1", ended_at=9_000,
                                           summary=summary_payload(1.25, run_id="run-1"))

        self.assertEqual(finalized.status, RunStatus.COMPLETED)
        self.assertEqual(finalized.ended_at.value, 9_000)
        target = self.root / RUNS_DIR / "run-1.json"
        self.assertTrue(target.exists())
        self.assertFalse(target.with_suffix(".json.tmp").exists())
        # 索引是 append-only：包含 start 与 finalize 两条事件
        events = [json.loads(line) for line in (self.root / INDEX_FILE).read_text().splitlines() if line.strip()]
        self.assertEqual([event["event"] for event in events], ["start", "finalize"])

    def test_unfinalized_run_is_incomplete_not_unknown(self) -> None:
        self.registry.start(runtime=runtime(), run_id="run-crash", now_ms=1_000, config=None)

        record = self.registry.load("run-crash")
        self.assertIsNotNone(record)
        self.assertEqual(record.status, RunStatus.INCOMPLETE)
        self.assertFalse(record.config_id.known)

    def test_finalizing_twice_is_refused(self) -> None:
        self.registry.start(runtime=runtime(), run_id="run-1", now_ms=1_000)
        self.registry.finalize(run_id="run-1", ended_at=2_000, summary=summary_payload(1.0, run_id="run-1"))

        with self.assertRaises(RunRegistryError):
            self.registry.finalize(run_id="run-1", ended_at=3_000, summary=None)

    def test_unknown_run_is_none_not_an_empty_record(self) -> None:
        self.assertIsNone(self.registry.load("nope"))

    def test_unknown_run_cannot_be_finalized(self) -> None:
        with self.assertRaises(RunRegistryError):
            self.registry.finalize(run_id="nope", ended_at=1, summary=None)

    def test_list_is_newest_first_and_never_deletes(self) -> None:
        self.registry.start(runtime=runtime("rt-old"), run_id="run-old", now_ms=1_000)
        self.registry.finalize(run_id="run-old", ended_at=1_500, summary=summary_payload(1.0, run_id="run-old"))
        self.registry.start(runtime=runtime("rt-new"), run_id="run-new", now_ms=5_000)

        records = self.registry.list()
        self.assertEqual([record.run_id for record in records], ["run-new", "run-old"])
        self.assertEqual(len(self.registry.list(limit=1)), 1)
        self.assertTrue((self.root / RUNS_DIR / "run-old.json").exists())

    def test_all_four_modes_can_be_registered(self) -> None:
        for index, mode in enumerate(RuntimeMode):
            self.registry.start(runtime=runtime(f"rt-{mode.value}", mode), run_id=f"run-{index}", now_ms=1_000 + index)
        self.assertEqual(len(self.registry.list()), 4)

    def test_corrupt_index_is_refused_not_guessed(self) -> None:
        self.registry.start(runtime=runtime(), run_id="run-1", now_ms=1_000)
        with (self.root / INDEX_FILE).open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")

        with self.assertRaises(RunRegistryError):
            self.registry.list()

    def test_compare_deltas_require_both_sides_known(self) -> None:
        self.registry.start(runtime=runtime(), run_id="run-a", now_ms=1_000)
        self.registry.finalize(run_id="run-a", ended_at=2_000, summary=summary_payload(1.0, run_id="run-a"))
        self.registry.start(runtime=runtime(), run_id="run-b", now_ms=3_000)
        self.registry.finalize(run_id="run-b", ended_at=4_000, summary=summary_payload(2.5, run_id="run-b"))

        comparison = self.registry.compare("run-a", "run-b")
        net = comparison.metric("net_pnl")
        self.assertTrue(net.delta.known)
        self.assertAlmostEqual(net.delta.value, 1.5)
        fees = comparison.metric("fees")
        self.assertTrue(fees.delta.known)
        self.assertAlmostEqual(fees.delta.value, 0.0)

    def test_compare_unknown_side_yields_unknown_delta(self) -> None:
        self.registry.start(runtime=runtime(), run_id="run-a", now_ms=1_000)
        self.registry.finalize(run_id="run-a", ended_at=2_000, summary=summary_payload(None, run_id="run-a"))
        self.registry.start(runtime=runtime(), run_id="run-b", now_ms=3_000)
        self.registry.finalize(run_id="run-b", ended_at=4_000, summary=summary_payload(2.0, run_id="run-b"))

        net = self.registry.compare("run-a", "run-b").metric("net_pnl")
        self.assertFalse(net.delta.known)
        self.assertIsNone(net.delta.value)
        self.assertIn("UNKNOWN on one side", net.delta.reason)

    def test_compare_with_a_missing_run_raises(self) -> None:
        self.registry.start(runtime=runtime(), run_id="run-a", now_ms=1_000)
        with self.assertRaises(RunRegistryError):
            self.registry.compare("run-a", "nope")

    def test_from_env_uses_override(self) -> None:
        import os

        original = os.environ.get("PROBEX_RUN_REGISTRY_DIR")
        os.environ["PROBEX_RUN_REGISTRY_DIR"] = str(self.root)
        try:
            self.assertEqual(JsonRunRegistry.from_env().root, self.root)
        finally:
            if original is None:
                os.environ.pop("PROBEX_RUN_REGISTRY_DIR", None)
            else:
                os.environ["PROBEX_RUN_REGISTRY_DIR"] = original


class RunLifecycleTest(unittest.TestCase):
    """裁决 B：runtime identity 创建时建 run；正常 stop 时 close；崩溃 ⇒ INCOMPLETE。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="probex-runs-life-"))
        self.registry = JsonRunRegistry(self.root)
        self.now = [1_000]

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def clock(self) -> int:
        return self.now[0]

    def test_normal_exit_completes_the_run(self) -> None:
        from reports.lifecycle import RunLifecycle

        with RunLifecycle(registry=self.registry, runtime=runtime(), run_id="run-1", clock=self.clock,
                          config=config()) as record:
            self.assertEqual(record.status, RunStatus.RUNNING)
            self.now[0] = 5_000

        stored = self.registry.load("run-1")
        self.assertEqual(stored.status, RunStatus.COMPLETED)
        self.assertEqual(stored.ended_at.value, 5_000)
        self.assertEqual(stored.config_id.value, "cfg-1")

    def test_crash_leaves_an_incomplete_run_and_reraises(self) -> None:
        from reports.lifecycle import RunLifecycle

        with self.assertRaises(RuntimeError):
            with RunLifecycle(registry=self.registry, runtime=runtime(), run_id="run-crash",
                              clock=self.clock):
                raise RuntimeError("boom")

        self.assertEqual(self.registry.load("run-crash").status, RunStatus.INCOMPLETE)

    def test_lifecycle_never_reuses_a_run_id(self) -> None:
        from reports.lifecycle import RunLifecycle
        from storage.run_registry import RunRegistryError

        with RunLifecycle(registry=self.registry, runtime=runtime(), run_id="run-1", clock=self.clock):
            pass
        with self.assertRaises(RunRegistryError):
            with RunLifecycle(registry=self.registry, runtime=runtime(), run_id="run-1", clock=self.clock):
                pass
