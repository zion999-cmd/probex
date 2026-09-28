"""P0001.9.4.2 Fault：HWM 的 fail-closed 路径（crash / 损坏 / 失效 / 前置条件不满足）。

核心命题：**High-Watermark 一旦建立，重启、午夜、crash 都不能把它洗掉**；
反过来，任何未知/失败都不得变成"drawdown = 0"。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from risk.high_watermark import (
    EquityHighWatermarkState,
    HighWatermarkError,
    HighWatermarkEvidence,
    HighWatermarkScope,
    HighWatermarkStatus,
    HighWatermarkTracker,
    state_to_mapping,
)
from storage.high_watermark import HighWatermarkStoreError, JsonHighWatermarkStore
from tests.readiness_support import (
    FailingHighWatermarkStore,
    InMemoryHighWatermarkStore,
    activated_state,
    satisfied_preconditions,
)
from tests.support import BASE_TS

DAY_MS = 86_400_000


class CrashWindowTest(unittest.TestCase):
    """SC-6 / SC-9：新 peak 的 durable 边界。"""

    def test_crash_before_durable_write_must_not_publish_peak(self) -> None:
        class CrashAfterSave(InMemoryHighWatermarkStore):
            """模拟：save 期间进程崩溃（save 未完成 ⇒ 不得发布）。"""

            def save(self, state: EquityHighWatermarkState) -> None:
                raise OSError("simulated crash during fsync")

        tracker = HighWatermarkTracker(state=activated_state(equity=1_000.0), store=CrashAfterSave())

        with self.assertRaises(OSError):
            tracker.observe_equity(current_equity=1_500.0, ts=BASE_TS + 1)

        self.assertEqual(tracker.state.peak_equity, 1_000.0)

    def test_restart_after_persisted_peak_restores_full_peak(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonHighWatermarkStore(Path(tmp) / "hwm.json")
            tracker = HighWatermarkTracker(state=None, store=store)
            tracker.activate(
                preconditions=satisfied_preconditions(), current_equity=5_000.0, activation_id="act-1", ts=BASE_TS
            )
            tracker.observe_equity(current_equity=5_400.0, ts=BASE_TS + 1)
            # "crash"：不调用任何清理逻辑，新进程直接从磁盘加载
            restarted = HighWatermarkTracker(state=store.load(), store=store)

            state = restarted.observe_equity(current_equity=5_100.0, ts=BASE_TS + 2)

            self.assertEqual(state.peak_equity, 5_400.0)  # 不低估 drawdown
            self.assertAlmostEqual(state.drawdown(current_equity=5_100.0) or 0.0, 300.0)

    def test_midnight_and_restart_do_not_reset_peak(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonHighWatermarkStore(Path(tmp) / "hwm.json")
            tracker = HighWatermarkTracker(state=None, store=store)
            tracker.activate(
                preconditions=satisfied_preconditions(), current_equity=5_200.0, activation_id="act-1", ts=BASE_TS
            )
            restarted = HighWatermarkTracker(state=store.load(), store=store)  # 跨日重启

            state = restarted.observe_equity(current_equity=5_000.0, ts=BASE_TS + DAY_MS)

            self.assertEqual(state.peak_equity, 5_200.0)
            self.assertAlmostEqual(state.drawdown(current_equity=5_000.0) or 0.0, 200.0)


class CorruptStateTest(unittest.TestCase):
    """SC-10：损坏 / 版本错误 / 缺字段 ⇒ 拒绝（绝不自动用当前 equity 重新初始化）。"""

    def _write(self, tmp: str, payload: object) -> JsonHighWatermarkStore:
        path = Path(tmp) / "hwm.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return JsonHighWatermarkStore(path)

    def test_corrupt_payloads_are_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            good = state_to_mapping(activated_state(equity=1.0))
            cases = [
                "not-json-at-all",
                {"schema_version": 1},  # 缺字段
                {"schema_version": 1, **{k: v for k, v in good.items() if k != "peak_equity"}},
                {"schema_version": 1, **{**good, "peak_equity": "not-a-number"}},
                {"schema_version": 1, **{**good, "status": "BOGUS"}},
                {"schema_version": 1, **{**good, "scope": "BOGUS"}},
                {"schema_version": 2, **good},  # 版本错误
            ]
            for payload in cases:
                path = Path(tmp) / "hwm.json"
                path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=str(payload)[:48]):
                    # schema 版本错误走 UnsupportedSchema 分支，其余走格式错误分支；两者都是 fail closed
                    with self.assertRaises(HighWatermarkStoreError):
                        JsonHighWatermarkStore(path).load()

    def test_corrupt_state_after_previous_activation_blocks_instead_of_resetting(self) -> None:
        """先激活并写入，再破坏文件：加载必须抛错（调用方 ⇒ BLOCKED），而不是得到"未初始化"。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hwm.json"
            store = JsonHighWatermarkStore(path)
            store.save(activated_state(equity=5_000.0))
            path.write_text('{"schema_version": 1, "scope": "TESTNET"}', encoding="utf-8")

            with self.assertRaises(HighWatermarkStoreError):
                store.load()

    def test_unknown_invalidation_kind_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hwm.json"
            state = activated_state(equity=1.0)
            invalidated = {
                **state_to_mapping(state),
                "status": HighWatermarkStatus.INVALIDATED.value,
                "invalidation_kind": "BOGUS",
            }
            path.write_text(json.dumps({"schema_version": 1, **invalidated}), encoding="utf-8")

            with self.assertRaises(HighWatermarkStoreError):
                JsonHighWatermarkStore(path).load()


class FailClosedReadinessTest(unittest.TestCase):
    """SC-7 / SC-13 / SC-14：不可用状态绝不放行。"""

    def test_store_failure_evidence_is_never_ready(self) -> None:
        tracker = HighWatermarkTracker(state=activated_state(equity=1.0), store=FailingHighWatermarkStore())

        with self.assertRaises(RuntimeError):
            tracker.observe_equity(current_equity=2.0, ts=BASE_TS + 1)

        evidence = HighWatermarkEvidence.store_failed(detail="injected")
        self.assertFalse(evidence.drawdown_known)
        self.assertIsNone(evidence.equity_observation())

    def test_invalidated_state_refuses_all_observations(self) -> None:
        tracker = HighWatermarkTracker(state=activated_state(equity=1.0), store=InMemoryHighWatermarkStore())
        tracker.invalidate_for_capital_flow(reason="TRANSFER", ts=BASE_TS + 1)

        with self.assertRaises(HighWatermarkError):
            tracker.observe_equity(current_equity=5.0, ts=BASE_TS + 2)
        self.assertIsNone(tracker.state.drawdown(current_equity=5.0))

    def test_activation_with_stale_or_mismatched_facts_is_refused(self) -> None:
        for overrides in (
            {"account_snapshot_fresh": False},
            {"equity_consistent": False},
            {"position_flat": False},
            {"open_orders_zero": False},
        ):
            with self.subTest(overrides=overrides):
                tracker = HighWatermarkTracker(state=None, store=InMemoryHighWatermarkStore())
                with self.assertRaises(HighWatermarkError):
                    tracker.activate(
                        preconditions=satisfied_preconditions(**overrides),
                        current_equity=1_000.0,
                        activation_id="act-1",
                        ts=BASE_TS,
                    )
                self.assertIsNone(tracker.state)

    def test_scope_isolation_is_explicit(self) -> None:
        mainnet = HighWatermarkTracker(state=None, store=InMemoryHighWatermarkStore())
        state = mainnet.activate(
            preconditions=satisfied_preconditions(scope=HighWatermarkScope.MAINNET, deployment_id="mainnet-1"),
            current_equity=10_000.0,
            activation_id="m1",
            ts=BASE_TS,
        )

        self.assertIs(state.scope, HighWatermarkScope.MAINNET)
        # testnet tracker 不能复用 mainnet 状态：由前置条件（scope 不匹配）拒绝新 activation
        testnet = HighWatermarkTracker(state=state, store=InMemoryHighWatermarkStore())
        with self.assertRaises(HighWatermarkError):
            testnet.activate(
                preconditions=satisfied_preconditions(scope=HighWatermarkScope.TESTNET, deployment_id="testnet-1"),
                current_equity=1_000.0,
                activation_id="t1",
                ts=BASE_TS + 1,
            )


if __name__ == "__main__":
    unittest.main()
