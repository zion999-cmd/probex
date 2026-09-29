"""F-15 retention：显式 policy / 只删已完成旧 run / active 保留 / 未配置 = UNBOUNDED。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports.types import RunStatus
from storage.retention import RetentionPolicy, prune_finished_runs
from storage.run_registry import JsonRunRegistry


def identity(runtime_id: str) -> RuntimeIdentity:
    return RuntimeIdentity(mode=RuntimeMode.PAPER, environment="local", venue="binance",
                           symbol="BTCUSDT", runtime_id=runtime_id, started_at=1_000,
                           data_timestamp=Fact.unknown("no data"))


class PolicyTest(unittest.TestCase):
    def test_all_none_is_unbounded(self) -> None:
        policy = RetentionPolicy()
        self.assertTrue(policy.is_unbounded)
        self.assertTrue(policy.to_payload()["unbounded"])

    def test_explicit_values_are_bounded(self) -> None:
        self.assertFalse(RetentionPolicy(run_max_runs=5).is_unbounded)

    def test_invalid_values_are_refused(self) -> None:
        for kwargs in ({"run_max_runs": -1}, {"run_max_runs": 0}, {"log_max_bytes": -5},
                       {"run_max_age_ms": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RetentionPolicy(**kwargs)


class PruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="probex-retention-"))
        self.registry = JsonRunRegistry(self.tmp)

    def _finish(self, index: int, *, ended_at: int) -> None:
        self.registry.start(runtime=identity(f"rt-{index}"), run_id=f"run-{index}", now_ms=1_000 + index)
        self.registry.finalize(run_id=f"run-{index}", ended_at=ended_at, summary={})

    def test_unbounded_policy_deletes_nothing(self) -> None:
        for index in range(4):
            self._finish(index, ended_at=2_000 + index)
        report = prune_finished_runs(self.registry, RetentionPolicy(), now_ms=10_000)
        self.assertFalse(report.applied)
        self.assertEqual(report.removed_run_ids, ())
        self.assertEqual(len(self.registry.list()), 4)

    def test_max_runs_keeps_the_newest_and_compacts_the_index(self) -> None:
        for index in range(5):
            self._finish(index, ended_at=2_000 + index)
        report = prune_finished_runs(self.registry, RetentionPolicy(run_max_runs=2), now_ms=10_000)
        self.assertTrue(report.applied)
        self.assertEqual(sorted(report.removed_run_ids), ["run-0", "run-1", "run-2"])
        remaining = {record.run_id for record in self.registry.list()}
        self.assertEqual(remaining, {"run-3", "run-4"})
        # index 一致：被删 run 不再可见，剩余 run 仍可读
        self.assertIsNone(self.registry.load("run-0"))
        self.assertIsNotNone(self.registry.load("run-3"))
        self.assertEqual(len(self.registry.finalized_runs()), 2)

    def test_max_age_prunes_only_old_finished_runs(self) -> None:
        self._finish(0, ended_at=1_000)
        self._finish(1, ended_at=9_000)
        report = prune_finished_runs(self.registry, RetentionPolicy(run_max_age_ms=5_000), now_ms=10_000)
        self.assertEqual(report.removed_run_ids, ("run-0",))
        self.assertEqual({record.run_id for record in self.registry.list()}, {"run-1"})

    def test_active_run_is_never_pruned(self) -> None:
        for index in range(3):
            self._finish(index, ended_at=2_000 + index)
        # 一个仍在运行（无 finalize）的 run
        self.registry.start(runtime=identity("rt-active"), run_id="run-active", now_ms=9_000)
        self.registry.mark_active(run_id="run-active", runtime_id="rt-active", mode="PAPER",
                                  symbol="BTCUSDT", now_ms=9_000)
        report = prune_finished_runs(self.registry, RetentionPolicy(run_max_runs=1), now_ms=10_000)
        self.assertNotIn("run-active", report.removed_run_ids)
        self.assertEqual(report.skipped_active_run_id, "run-active")
        # running run 的 start 事件仍在 index 中（未被压缩掉）
        self.assertIsNotNone(self.registry.load("run-active"))
        self.assertIs(self.registry.load("run-active").status, RunStatus.RUNNING)

    def test_completed_finalization_is_preserved_for_kept_runs(self) -> None:
        for index in range(3):
            self._finish(index, ended_at=2_000 + index)
        prune_finished_runs(self.registry, RetentionPolicy(run_max_runs=1), now_ms=10_000)
        record = self.registry.load("run-2")
        self.assertIs(record.status, RunStatus.COMPLETED)
        self.assertTrue(record.ended_at.known)


class PidReuseTest(unittest.TestCase):
    """F-15：陈旧 marker / PID 复用不得直接判为 RUNNING（不是 TTL 判断）。"""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="probex-pid-"))
        self.registry = JsonRunRegistry(self.tmp)

    def test_marker_with_wrong_process_start_is_not_active(self) -> None:
        import os

        self.registry.start(runtime=identity("rt-1"), run_id="run-1", now_ms=1_000)
        marker = self.registry.mark_active(run_id="run-1", runtime_id="rt-1", mode="PAPER",
                                           symbol="BTCUSDT", now_ms=1_000, pid=os.getpid())
        self.assertEqual(self.registry.active_run_id(), "run-1")   # 真 marker
        self.assertIsNotNone(marker.get("process_started_at_ms"))
        # 篡改启动时刻 ⇒ 视为 PID 复用 / 陈旧 marker
        import json

        path = self.tmp / "active.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["process_started_at_ms"] = int(payload["process_started_at_ms"]) - 3_600_000
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(self.registry.active_run_id())

    def test_process_alive_helper(self) -> None:
        import os

        from storage.run_registry import process_is_alive

        self.assertTrue(process_is_alive(os.getpid()))
        self.assertFalse(process_is_alive(999_999))
        self.assertFalse(process_is_alive(-1))

    def test_dead_pid_is_not_active(self) -> None:
        self.registry.start(runtime=identity("rt-2"), run_id="run-2", now_ms=1_000)
        self.registry.mark_active(run_id="run-2", runtime_id="rt-2", mode="PAPER", symbol="BTCUSDT",
                                  now_ms=1_000, pid=999_999)
        self.assertIsNone(self.registry.active_run_id())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
