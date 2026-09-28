"""P0001.9.4.2 单元测试：Equity High-Watermark 领域契约与 durable store。

SC 对应：

- SC-1 未 activation → 状态 UNINITIALIZED（readiness 侧由 `test_readiness_gate` 覆盖）
- SC-2 activation 前置条件必须全部满足
- SC-3 activation 后 peak = activation equity、drawdown = 0，且标记 since activation
- SC-4/5 peak 单调：只升不降；回落时 drawdown 正确增加
- SC-6 新 peak 必须先 durable 成功才发布
- SC-7 持久化失败 ⇒ 不发布（fail closed）
- SC-8/9 重启/崩溃后加载旧 peak，不用当前 equity 重置
- SC-10 缺失/损坏/schema 错误 ⇒ 拒绝（不自动重新初始化）
- SC-11 testnet state 不能加载到 mainnet
- SC-12 UTC 日界不重置 peak
- SC-13 TRANSFER 后 INVALIDATED
- SC-16 rebase 显式、新 activation id
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from risk.high_watermark import (
    ActivationPreconditions,
    EquityHighWatermarkState,
    HighWatermarkError,
    HighWatermarkEvidence,
    HighWatermarkInvalidation,
    HighWatermarkScope,
    HighWatermarkStatus,
    HighWatermarkTracker,
    state_from_mapping,
    state_to_mapping,
)
from storage.high_watermark import (
    SCHEMA_VERSION,
    HighWatermarkStoreError,
    JsonHighWatermarkStore,
)
from tests.readiness_support import (
    FailingHighWatermarkStore,
    InMemoryHighWatermarkStore,
    activated_state,
    satisfied_preconditions,
)
from tests.support import BASE_TS

DAY_MS = 86_400_000


class ActivationTest(unittest.TestCase):
    def test_sc2_every_precondition_is_required(self) -> None:
        cases = {
            "recovery": {"recovery_recovered": False},
            "position": {"position_flat": False},
            "orders": {"open_orders_zero": False},
            "unresolved": {"unresolved_orders_zero": False},
            "daily_pnl": {"daily_pnl_known": False},
            "equity": {"current_equity_known": False},
            "snapshot": {"account_snapshot_fresh": False},
            "mismatch": {"equity_consistent": False},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                tracker = HighWatermarkTracker(state=None, store=InMemoryHighWatermarkStore())
                with self.assertRaises(HighWatermarkError) as ctx:
                    tracker.activate(
                        preconditions=satisfied_preconditions(**overrides),
                        current_equity=1_000.0,
                        activation_id="act-1",
                        ts=BASE_TS,
                    )
                self.assertIn("preconditions unmet", str(ctx.exception))
                self.assertIsNone(tracker.state)  # 失败不得留下半成品状态

    def test_sc3_activation_sets_peak_to_current_equity(self) -> None:
        tracker = HighWatermarkTracker(state=None, store=InMemoryHighWatermarkStore())

        state = tracker.activate(
            preconditions=satisfied_preconditions(), current_equity=5_000.0, activation_id="act-1", ts=BASE_TS
        )

        self.assertEqual(state.peak_equity, 5_000.0)
        self.assertEqual(state.activation_equity, 5_000.0)
        self.assertEqual(state.drawdown(current_equity=5_000.0), 0.0)
        self.assertEqual(state.drawdown_pct(current_equity=5_000.0), 0.0)
        self.assertEqual(state.activation_ts, BASE_TS)
        self.assertIs(state.status, HighWatermarkStatus.ACTIVE)
        self.assertEqual(state.generation, 1)

    def test_activation_is_never_automatic(self) -> None:
        """从未激活 ⇒ 状态为空（调用方必须显式 activate）。"""
        tracker = HighWatermarkTracker(state=None, store=InMemoryHighWatermarkStore())

        self.assertIsNone(tracker.state)
        self.assertIs(tracker.status, HighWatermarkStatus.UNINITIALIZED)
        self.assertIs(tracker.evidence().status, HighWatermarkStatus.UNINITIALIZED)
        with self.assertRaises(HighWatermarkError):
            tracker.observe_equity(current_equity=1.0, ts=BASE_TS)

    def test_reactivation_on_active_epoch_is_refused(self) -> None:
        tracker = HighWatermarkTracker(state=None, store=InMemoryHighWatermarkStore())
        tracker.activate(
            preconditions=satisfied_preconditions(), current_equity=1_000.0, activation_id="act-1", ts=BASE_TS
        )
        tracker.observe_equity(current_equity=1_200.0, ts=BASE_TS + 10)

        with self.assertRaises(HighWatermarkError):
            tracker.activate(
                preconditions=satisfied_preconditions(), current_equity=900.0, activation_id="act-2", ts=BASE_TS + 20
            )

        self.assertEqual(tracker.state.peak_equity, 1_200.0)  # 未被洗掉

    def test_activation_arguments_are_validated(self) -> None:
        tracker = HighWatermarkTracker(state=None, store=InMemoryHighWatermarkStore())
        for kwargs in (
            {"current_equity": float("nan")},
            {"current_equity": True},
            {"activation_id": ""},
            {"ts": -1},
        ):
            with self.subTest(kwargs=kwargs):
                values: dict[str, object] = {
                    "preconditions": satisfied_preconditions(),
                    "current_equity": 1_000.0,
                    "activation_id": "act-1",
                    "ts": BASE_TS,
                }
                values.update(kwargs)
                with self.assertRaises(HighWatermarkError):
                    tracker.activate(**values)  # type: ignore[arg-type]


class MonotonicPeakTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryHighWatermarkStore()
        self.tracker = HighWatermarkTracker(state=None, store=self.store)
        self.tracker.activate(
            preconditions=satisfied_preconditions(), current_equity=5_000.0, activation_id="act-1", ts=BASE_TS
        )

    def test_sc4_peak_rises_with_equity(self) -> None:
        state = self.tracker.observe_equity(current_equity=5_100.0, ts=BASE_TS + 1)

        self.assertEqual(state.peak_equity, 5_100.0)
        self.assertEqual(state.peak_ts, BASE_TS + 1)
        self.assertEqual(state.drawdown(current_equity=5_100.0), 0.0)

    def test_sc5_peak_never_falls(self) -> None:
        self.tracker.observe_equity(current_equity=5_100.0, ts=BASE_TS + 1)

        state = self.tracker.observe_equity(current_equity=4_900.0, ts=BASE_TS + 2)

        self.assertEqual(state.peak_equity, 5_100.0)
        self.assertEqual(state.last_equity, 4_900.0)
        self.assertAlmostEqual(state.drawdown(current_equity=4_900.0) or 0.0, 200.0)
        self.assertAlmostEqual(state.drawdown_pct(current_equity=4_900.0) or 0.0, 200.0 / 5_100.0)

    def test_equal_equity_does_not_move_peak_ts(self) -> None:
        self.tracker.observe_equity(current_equity=5_000.0, ts=BASE_TS + 5)

        state = self.tracker.state

        self.assertEqual(state.peak_ts, BASE_TS)  # 未创新高 ⇒ peak_ts 保持

    def test_sc6_peak_is_persisted_before_publish(self) -> None:
        order: list[str] = []

        class RecordingStore(InMemoryHighWatermarkStore):
            def save(self, state: EquityHighWatermarkState) -> None:
                order.append(f"save:{state.peak_equity}")
                super().save(state)

        tracker = HighWatermarkTracker(state=None, store=RecordingStore())
        tracker.activate(
            preconditions=satisfied_preconditions(), current_equity=1_000.0, activation_id="act-1", ts=BASE_TS
        )
        before = tracker.state.peak_equity

        tracker.observe_equity(current_equity=1_100.0, ts=BASE_TS + 1)

        self.assertEqual(order[-1], "save:1100.0")  # durable 先发生
        self.assertEqual(before, 1_000.0)
        self.assertEqual(tracker.state.peak_equity, 1_100.0)

    def test_sc7_store_failure_does_not_publish_new_peak(self) -> None:
        tracker = HighWatermarkTracker(state=activated_state(equity=1_000.0), store=FailingHighWatermarkStore())

        with self.assertRaises(RuntimeError):
            tracker.observe_equity(current_equity=1_500.0, ts=BASE_TS + 1)

        self.assertEqual(tracker.state.peak_equity, 1_000.0)  # 未发布

    def test_sc12_utc_midnight_does_not_reset_peak(self) -> None:
        tracker = HighWatermarkTracker(
            state=activated_state(equity=5_200.0, ts=BASE_TS),
            store=InMemoryHighWatermarkStore(),
        )
        next_day = BASE_TS + DAY_MS  # 跨 UTC 日界

        state = tracker.observe_equity(current_equity=5_000.0, ts=next_day)

        self.assertEqual(state.peak_equity, 5_200.0)
        self.assertAlmostEqual(state.drawdown(current_equity=5_000.0) or 0.0, 200.0)

    def test_observation_before_activation_is_refused(self) -> None:
        with self.assertRaises(HighWatermarkError):
            self.tracker.observe_equity(current_equity=1.0, ts=BASE_TS - 1)


class RestartTest(unittest.TestCase):
    def test_sc8_restart_loads_previous_peak(self) -> None:
        store = InMemoryHighWatermarkStore()
        first = HighWatermarkTracker(state=None, store=store)
        first.activate(
            preconditions=satisfied_preconditions(), current_equity=5_000.0, activation_id="act-1", ts=BASE_TS
        )
        first.observe_equity(current_equity=5_300.0, ts=BASE_TS + 1)

        # "重启"：新进程只从 store 加载
        restarted = HighWatermarkTracker(state=store.load(), store=store)
        state = restarted.observe_equity(current_equity=5_100.0, ts=BASE_TS + 2)

        self.assertEqual(state.peak_equity, 5_300.0)  # 绝不用 current equity 重置
        self.assertAlmostEqual(state.drawdown(current_equity=5_100.0) or 0.0, 200.0)

    def test_restarted_tracker_refuses_to_activate_over_live_epoch(self) -> None:
        store = InMemoryHighWatermarkStore()
        tracker = HighWatermarkTracker(state=activated_state(equity=7_000.0), store=store)
        restarted = HighWatermarkTracker(state=store.load() or tracker.state, store=store)

        with self.assertRaises(HighWatermarkError):
            restarted.activate(
                preconditions=satisfied_preconditions(), current_equity=6_000.0, activation_id="act-9", ts=BASE_TS + 9
            )

        self.assertEqual(restarted.state.peak_equity, 7_000.0)


class InvalidationAndRebaseTest(unittest.TestCase):
    def test_sc13_capital_flow_invalidates_and_blocks_observations(self) -> None:
        tracker = HighWatermarkTracker(state=activated_state(equity=5_000.0), store=InMemoryHighWatermarkStore())

        state = tracker.invalidate_for_capital_flow(reason="TRANSFER detected", ts=BASE_TS + 1)

        self.assertIs(state.status, HighWatermarkStatus.INVALIDATED)
        self.assertIs(state.invalidation_kind, HighWatermarkInvalidation.CAPITAL_FLOW)
        self.assertFalse(tracker.evidence().drawdown_known)
        self.assertIsNone(state.drawdown(current_equity=4_000.0))
        with self.assertRaises(HighWatermarkError):
            tracker.observe_equity(current_equity=6_000.0, ts=BASE_TS + 2)

    def test_sc16_rebase_requires_explicit_authorization_and_new_id(self) -> None:
        tracker = HighWatermarkTracker(state=activated_state(equity=5_000.0), store=InMemoryHighWatermarkStore())
        tracker.invalidate_for_capital_flow(reason="TRANSFER", ts=BASE_TS + 1)

        with self.assertRaises(HighWatermarkError):  # 前置条件未满足 ⇒ 仍拒绝
            tracker.rebase(
                preconditions=satisfied_preconditions(position_flat=False),
                current_equity=7_000.0,
                activation_id="act-2",
                ts=BASE_TS + 2,
                reason="human rebase",
            )
        with self.assertRaises(HighWatermarkError):  # 复用旧 activation id ⇒ 拒绝
            tracker.rebase(
                preconditions=satisfied_preconditions(),
                current_equity=7_000.0,
                activation_id="act-1",
                ts=BASE_TS + 2,
                reason="human rebase",
            )

        state = tracker.rebase(
            preconditions=satisfied_preconditions(),
            current_equity=7_000.0,
            activation_id="act-2",
            ts=BASE_TS + 2,
            reason="human rebase",
        )

        self.assertEqual(state.activation_id, "act-2")
        self.assertEqual(state.peak_equity, 7_000.0)
        self.assertEqual(state.generation, 2)  # 新的 epoch generation
        self.assertEqual(state.invalidation_reason, "human rebase")

    def test_sc14_external_flow_is_not_misread_as_drawdown(self) -> None:
        """withdraw 2000 让 equity 下降：HWM 不下降，但也没有被当成 trading drawdown 放行。"""
        tracker = HighWatermarkTracker(state=activated_state(equity=5_000.0), store=InMemoryHighWatermarkStore())

        state = tracker.invalidate_for_capital_flow(reason="withdraw detected", ts=BASE_TS + 1)

        self.assertIs(state.status, HighWatermarkStatus.INVALIDATED)
        self.assertIsNone(state.drawdown(current_equity=3_000.0))  # 未知 ⇒ 不会误报 2000 的 drawdown

    def test_invalidate_requires_activation_and_reason(self) -> None:
        tracker = HighWatermarkTracker(state=None, store=InMemoryHighWatermarkStore())
        with self.assertRaises(HighWatermarkError):
            tracker.invalidate_for_capital_flow(reason="x", ts=BASE_TS)
        activated = HighWatermarkTracker(state=activated_state(), store=InMemoryHighWatermarkStore())
        with self.assertRaises(HighWatermarkError):
            activated.invalidate_for_capital_flow(reason="", ts=BASE_TS)


class StateContractTest(unittest.TestCase):
    def test_monotonic_invariants_are_enforced_on_construction(self) -> None:
        with self.assertRaises(HighWatermarkError):
            EquityHighWatermarkState(
                scope=HighWatermarkScope.TESTNET,
                deployment_id="d",
                activation_id="a",
                activation_ts=10,
                activation_equity=100.0,
                peak_equity=50.0,  # 低于 activation ⇒ 非法
                peak_ts=10,
                last_equity=100.0,
                last_observed_ts=10,
                capital_flow_checked_through=10,
                generation=1,
                status=HighWatermarkStatus.ACTIVE,
            )
        with self.assertRaises(HighWatermarkError):
            EquityHighWatermarkState(
                scope=HighWatermarkScope.TESTNET,
                deployment_id="d",
                activation_id="a",
                activation_ts=10,
                activation_equity=100.0,
                peak_equity=100.0,
                peak_ts=10,
                last_equity=120.0,  # 高于 peak ⇒ 非法
                last_observed_ts=11,
                capital_flow_checked_through=10,
                generation=1,
                status=HighWatermarkStatus.ACTIVE,
            )

    def test_invalidated_state_requires_a_kind(self) -> None:
        with self.assertRaises(HighWatermarkError):
            EquityHighWatermarkState(
                scope=HighWatermarkScope.TESTNET,
                deployment_id="d",
                activation_id="a",
                activation_ts=10,
                activation_equity=100.0,
                peak_equity=100.0,
                peak_ts=10,
                last_equity=100.0,
                last_observed_ts=10,
                capital_flow_checked_through=10,
                generation=1,
                status=HighWatermarkStatus.INVALIDATED,
                invalidation_reason="x",
            )

    def test_mapping_round_trip(self) -> None:
        state = activated_state(equity=1_234.5)

        restored = state_from_mapping(state_to_mapping(state))

        self.assertEqual(restored, state)

    def test_scope_and_status_are_typed(self) -> None:
        for scope in (HighWatermarkScope.TESTNET, HighWatermarkScope.MAINNET):
            tracked = HighWatermarkTracker(
                state=None, store=InMemoryHighWatermarkStore()
            ).activate(
                preconditions=satisfied_preconditions(scope=scope, deployment_id=f"dep-{scope.value}"),
                current_equity=100.0,
                activation_id="a",
                ts=BASE_TS,
            )
            with self.subTest(scope=scope):
                self.assertIs(tracked.scope, scope)
                self.assertEqual(tracked.deployment_id, f"dep-{scope.value}")


class DurableStoreTest(unittest.TestCase):
    def test_round_trip_and_atomic_write(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "hwm.json"
            store = JsonHighWatermarkStore(path)
            self.assertIsNone(store.load())
            state = activated_state(equity=5_000.0)

            store.save(state)

            self.assertEqual(store.load(), state)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            self.assertFalse(list(Path(tmp).glob("nested/.hwm-*.tmp")), "temp file must be replaced atomically")

    def test_sc10_corrupt_or_unknown_schema_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hwm.json"
            store = JsonHighWatermarkStore(path)
            for content in ("not json", "[]", '{"schema_version": 99}', '{"schema_version": 1}'):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content):
                    with self.assertRaises(HighWatermarkStoreError):
                        store.load()

    def test_sc10_missing_file_means_uninitialized_not_reset(self) -> None:
        """删除状态文件 ≠ 重置：读到的只是"从未激活"，必须显式 activation（readiness 因此仍 BLOCKED）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hwm.json"
            store = JsonHighWatermarkStore(path)
            store.save(activated_state(equity=5_000.0))
            path.unlink()

            self.assertIsNone(store.load())
            tracker = HighWatermarkTracker(state=store.load(), store=store)
            self.assertIs(tracker.evidence().problem, None)
            self.assertIs(tracker.evidence().status, HighWatermarkStatus.UNINITIALIZED)

    def test_sc11_scope_mismatch_is_detected_by_caller(self) -> None:
        """store 只负责读写；scope 校验由调用方在 activation 前置条件上完成（testnet/mainnet 不互载）。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hwm.json"
            store = JsonHighWatermarkStore(path)
            store.save(activated_state(equity=1.0))  # TESTNET

            loaded = store.load()

            self.assertIs(loaded.scope, HighWatermarkScope.TESTNET)
            with self.assertRaises(HighWatermarkError):
                HighWatermarkTracker(state=loaded, store=store).activate(
                    preconditions=satisfied_preconditions(scope=HighWatermarkScope.MAINNET, deployment_id="mainnet"),
                    current_equity=1.0,
                    activation_id="mainnet-act",
                    ts=BASE_TS + 1,
                )

    def test_write_failure_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hwm.json"
            path.mkdir()  # 用目录占位 ⇒ 写入必然失败
            store = JsonHighWatermarkStore(path)

            with self.assertRaises(HighWatermarkStoreError):
                store.save(activated_state())


class EvidenceTest(unittest.TestCase):
    def test_active_evidence_exposes_observation(self) -> None:
        evidence = HighWatermarkEvidence.from_state(activated_state(equity=2_000.0, ts=BASE_TS))

        self.assertTrue(evidence.drawdown_known)
        self.assertEqual(evidence.equity_observation(), (2_000.0, BASE_TS))
        self.assertEqual(evidence.activation_equity, 2_000.0)
        self.assertEqual(evidence.generation, 1)

    def test_inactive_evidence_hides_peak(self) -> None:
        tracker = HighWatermarkTracker(state=activated_state(equity=2_000.0), store=InMemoryHighWatermarkStore())
        tracker.invalidate_for_capital_flow(reason="TRANSFER", ts=BASE_TS + 1)

        evidence = tracker.evidence()

        self.assertFalse(evidence.drawdown_known)
        self.assertIsNone(evidence.equity_observation())
        self.assertIsNone(evidence.peak_equity)

    def test_uninitialized_helper_is_explicit(self) -> None:
        evidence = HighWatermarkEvidence.uninitialized()

        self.assertIs(evidence.status, HighWatermarkStatus.UNINITIALIZED)
        self.assertIsNone(evidence.peak_equity)
        self.assertFalse(evidence.drawdown_known)


class PreconditionsContractTest(unittest.TestCase):
    def test_unmet_lists_every_problem(self) -> None:
        preconditions = ActivationPreconditions(
            recovery_recovered=False,
            position_flat=False,
            open_orders_zero=False,
            unresolved_orders_zero=False,
            daily_pnl_known=False,
            current_equity_known=False,
            account_snapshot_fresh=False,
            equity_consistent=False,
            scope=HighWatermarkScope.TESTNET,
            deployment_id="d",
        )

        self.assertEqual(
            set(preconditions.unmet),
            {
                "recovery_not_recovered",
                "position_not_flat",
                "open_orders_present",
                "unresolved_orders_present",
                "daily_pnl_unknown",
                "equity_unknown",
                "account_snapshot_stale",
                "equity_mismatch",
            },
        )
        self.assertFalse(preconditions.satisfied)

    def test_flags_must_be_bool_and_scope_typed(self) -> None:
        with self.assertRaises(HighWatermarkError):
            satisfied_preconditions(recovery_recovered="yes")
        with self.assertRaises(HighWatermarkError):
            satisfied_preconditions(scope="TESTNET")
        with self.assertRaises(HighWatermarkError):
            satisfied_preconditions(deployment_id="")


if __name__ == "__main__":
    unittest.main()
