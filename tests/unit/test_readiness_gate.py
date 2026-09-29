"""P0001.9.4 SC-1 / SC-6 / SC-8 / SC-9 / SC-10 / SC-11：Live Readiness Gate 单元测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from connectors.binance.private.auth import ClockCalibration
from connectors.binance.private.recovery import RecoveryStatus
from readiness import (
    AccountEvidence,
    Environment,
    EnvironmentEvidence,
    EnvironmentValidationEvidence,
    EnvironmentValidationStatus,
    HistoricalRiskEvidence,
    LiveReadinessEvidence,
    LiveReadinessGate,
    LiveReadinessReason,
    LiveReadinessScope,
    LiveReadinessStatus,
    LiveRiskPolicy,
    PrivateLatencyStatus,
    PrivateStreamEvidence,
    ReadinessError,
    ReadinessPolicy,
)
from risk.high_watermark import (
    HighWatermarkEvidence,
    HighWatermarkInvalidation,
    HighWatermarkScope,
    HighWatermarkStatus,
)
from risk.types import KillSwitchMode
from tests.readiness_support import (
    active_hwm,
    activated_tracker,
    environment_evidence,
    market_evidence,
    satisfied_preconditions,
)
from tests.support import BASE_TS

NOW = BASE_TS + 1_000
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def calibration(*, offset_ms: int = 0, round_trip_ms: int = 40, measured_at_ms: int | None = None) -> ClockCalibration:
    return ClockCalibration(
        offset_ms=offset_ms,
        round_trip_ms=round_trip_ms,
        uncertainty_ms=round_trip_ms // 2,
        measured_at_ms=NOW - 100 if measured_at_ms is None else measured_at_ms,
    )


def readiness_policy(**overrides: object) -> ReadinessPolicy:
    values: dict[str, object] = {
        "max_clock_uncertainty_ms": 100,
        "max_median_private_lag_ms": 2_000,
        "max_calibration_age_ms": 600_000,
        "max_available_balance_age_ms": 30_000,
    }
    values.update(overrides)
    return ReadinessPolicy(**values)  # type: ignore[arg-type]


def risk_policy(**overrides: object) -> LiveRiskPolicy:
    values: dict[str, object] = {
        "max_position_qty": 0.01,
        "max_order_notional": 500.0,
        "max_open_order_exposure": 500.0,
        "max_daily_loss": 50.0,
        "max_drawdown_pct": 0.10,
        "max_leverage": 3.0,
        "max_mark_age_ms": 5_000,
    }
    values.update(overrides)
    return LiveRiskPolicy(**values)  # type: ignore[arg-type]


def evidence(**overrides: object) -> LiveReadinessEvidence:
    values: dict[str, object] = {
        "now_ms": NOW,
        "recovery_status": RecoveryStatus.RECOVERED,
        "private_stream": PrivateStreamEvidence(
            listen_key_state="ACTIVE",
            continuity_assumed=True,
            boundary_present=True,
            median_private_lag_ms=50,
            clock_calibration=calibration(),
        ),
        "account": AccountEvidence(can_trade=True, available_balance=1_000.0, available_balance_captured_at=NOW - 500),
        "historical_risk": HistoricalRiskEvidence(daily_pnl_known=True, drawdown_known=True, peak_equity_known=True),
        "environment": environment_evidence(environment=Environment.TESTNET),
        "market": market_evidence(),
        "risk_policy": risk_policy(),
        # P0001.9.4.2：默认给出**已确认**的 durable HWM（否则 drawdown 必然未知 ⇒ BLOCKED）
        "high_watermark": active_hwm(),
    }
    values.update(overrides)
    return LiveReadinessEvidence(**values)  # type: ignore[arg-type]


class ReadyBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = LiveReadinessGate(policy=readiness_policy())

    def test_all_conditions_met_is_live_ready_for_testnet(self) -> None:
        result = self.gate.evaluate(evidence())

        self.assertIs(result.status, LiveReadinessStatus.LIVE_READY)
        self.assertIs(result.scope, LiveReadinessScope.TESTNET_LIVE_READY)  # SC-9：作用域受限
        self.assertEqual(result.reasons, ())
        self.assertTrue(result.live_ready)
        self.assertFalse(result.blocked)

    def test_evaluate_is_pure_and_repeatable(self) -> None:
        item = evidence()

        first = self.gate.evaluate(item)
        second = self.gate.evaluate(item)

        self.assertEqual(first, second)

    def test_policy_is_required_and_has_no_defaults(self) -> None:
        with self.assertRaises(TypeError):
            ReadinessPolicy()  # type: ignore[call-arg]
        with self.assertRaises(ReadinessError):
            readiness_policy(max_clock_uncertainty_ms=-1)
        with self.assertRaises(ReadinessError):
            LiveReadinessGate(policy="not-a-policy")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            LiveRiskPolicy(max_position_qty=1.0)  # type: ignore[call-arg]


class Sc1HistoricalRiskTest(unittest.TestCase):
    """SC-1：RECOVERED=true 但历史风险 UNKNOWN ⇒ BLOCKED（这是正确结果，不是失败）。"""

    def setUp(self) -> None:
        self.gate = LiveReadinessGate(policy=readiness_policy())

    def test_recovered_but_history_unknown_is_blocked(self) -> None:
        result = self.gate.evaluate(
            evidence(
                historical_risk=HistoricalRiskEvidence(
                    daily_pnl_known=False, drawdown_known=False, peak_equity_known=False
                )
            )
        )

        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)
        self.assertIsNone(result.scope)
        self.assertEqual(
            result.reasons,
            (
                LiveReadinessReason.HISTORICAL_DAILY_PNL_UNKNOWN,
                LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN,
            ),
        )

    def test_drawdown_unknown_alone_is_blocked(self) -> None:
        result = self.gate.evaluate(
            evidence(
                historical_risk=HistoricalRiskEvidence(
                    daily_pnl_known=True, drawdown_known=False, peak_equity_known=True
                )
            )
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN,))

    def test_peak_unknown_alone_is_blocked(self) -> None:
        result = self.gate.evaluate(
            evidence(
                historical_risk=HistoricalRiskEvidence(
                    daily_pnl_known=True, drawdown_known=True, peak_equity_known=False
                )
            )
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN,))


class RecoveryAndStreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = LiveReadinessGate(policy=readiness_policy())

    def test_recovery_not_ready_blocks(self) -> None:
        for status in (RecoveryStatus.NOT_RECOVERED, RecoveryStatus.BLOCKED):
            with self.subTest(status=status):
                result = self.gate.evaluate(evidence(recovery_status=status))
                self.assertIn(LiveReadinessReason.RECOVERY_NOT_READY, result.reasons)

    def test_stream_not_ready_blocks(self) -> None:
        cases = {
            "listenKey": PrivateStreamEvidence("STOPPED", True, True, 50, calibration()),
            "continuity": PrivateStreamEvidence("ACTIVE", False, True, 50, calibration()),
            "boundary": PrivateStreamEvidence("ACTIVE", True, False, 50, calibration()),
        }
        for label, stream in cases.items():
            with self.subTest(case=label):
                result = self.gate.evaluate(evidence(private_stream=stream))
                self.assertIn(LiveReadinessReason.PRIVATE_STREAM_NOT_READY, result.reasons)

    def test_unobserved_latency_is_not_treated_as_ok(self) -> None:
        """P0001.9.7.1：无样本且无不可测事件 ⇒ `UNOBSERVED`（干净冷启动），**不是** `LIVE_READY`。"""
        result = self.gate.evaluate(
            evidence(private_stream=PrivateStreamEvidence("ACTIVE", True, True, None, calibration()))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED,))
        self.assertIs(result.latency_status, PrivateLatencyStatus.UNOBSERVED)
        self.assertFalse(result.live_ready)
        self.assertIs(result.status, LiveReadinessStatus.BOOTSTRAP_ELIGIBLE)
        self.assertTrue(result.bootstrap_eligible)

    def test_unobserved_latency_with_any_other_problem_is_blocked(self) -> None:
        """只有"唯一原因是 UNOBSERVED"才可能 bootstrap；否则一律 BLOCKED。"""
        result = self.gate.evaluate(
            evidence(
                private_stream=PrivateStreamEvidence("ACTIVE", True, True, None, calibration()),
                account=AccountEvidence(can_trade=False, available_balance=100.0,
                                        available_balance_captured_at=0),
            )
        )

        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)
        self.assertFalse(result.bootstrap_eligible)
        self.assertIn(LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED, result.reasons)
        self.assertIn(LiveReadinessReason.ACCOUNT_CANNOT_TRADE, result.reasons)

    def test_unmeasurable_events_are_unknown_not_unobserved(self) -> None:
        """P0001.9.7.1：有事件到达但无法测量 ⇒ `UNKNOWN`（测量链异常）⇒ fail closed。"""
        result = self.gate.evaluate(
            evidence(
                private_stream=PrivateStreamEvidence("ACTIVE", True, True, None, calibration(), 0, 3)
            )
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.PRIVATE_LATENCY_UNKNOWN,))
        self.assertIs(result.latency_status, PrivateLatencyStatus.UNKNOWN)
        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)

    def test_healthy_latency_status_is_exposed(self) -> None:
        result = self.gate.evaluate(
            evidence(private_stream=PrivateStreamEvidence("ACTIVE", True, True, 25, calibration(), 12, 0))
        )

        self.assertIs(result.latency_status, PrivateLatencyStatus.HEALTHY)

    def test_high_latency_blocks(self) -> None:
        result = self.gate.evaluate(
            evidence(private_stream=PrivateStreamEvidence("ACTIVE", True, True, 2_001, calibration()))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.PRIVATE_LATENCY_TOO_HIGH,))

    def test_market_not_ready_blocks(self) -> None:
        result = self.gate.evaluate(evidence(market=market_evidence(ready=False)))

        self.assertEqual(result.reasons, (LiveReadinessReason.MARKET_NOT_READY,))
        self.assertIn("book_health=stale", result.details[0])


class Sc8ClockTest(unittest.TestCase):
    """SC-8：未测量 / 过旧 / uncertainty 超阈值 ⇒ BLOCKED。"""

    def setUp(self) -> None:
        self.gate = LiveReadinessGate(policy=readiness_policy())

    def test_missing_calibration_blocks(self) -> None:
        result = self.gate.evaluate(
            evidence(private_stream=PrivateStreamEvidence("ACTIVE", True, True, 50, None))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.CLOCK_NOT_CALIBRATED,))

    def test_stale_calibration_blocks(self) -> None:
        stale = calibration(measured_at_ms=NOW - 600_001)

        result = self.gate.evaluate(
            evidence(private_stream=PrivateStreamEvidence("ACTIVE", True, True, 50, stale))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.CLOCK_NOT_CALIBRATED,))

    def test_high_uncertainty_blocks_even_with_small_corrected_lag(self) -> None:
        """SC-7 / §6：不能因为 corrected lag 很小就忽略巨大的 RTT。"""
        noisy = ClockCalibration(offset_ms=0, round_trip_ms=4_000, uncertainty_ms=2_000, measured_at_ms=NOW - 10)

        result = self.gate.evaluate(
            evidence(private_stream=PrivateStreamEvidence("ACTIVE", True, True, 1, noisy))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.CLOCK_UNCERTAINTY_TOO_HIGH,))

    def test_uncertainty_boundary_is_inclusive(self) -> None:
        exact = ClockCalibration(offset_ms=0, round_trip_ms=200, uncertainty_ms=100, measured_at_ms=NOW - 10)

        result = self.gate.evaluate(
            evidence(private_stream=PrivateStreamEvidence("ACTIVE", True, True, 1, exact))
        )

        self.assertIs(result.status, LiveReadinessStatus.LIVE_READY)


class AccountEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = LiveReadinessGate(policy=readiness_policy())

    def test_cannot_trade_blocks(self) -> None:
        result = self.gate.evaluate(
            evidence(account=AccountEvidence(can_trade=False, available_balance=1.0, available_balance_captured_at=NOW))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.ACCOUNT_CANNOT_TRADE,))

    def test_missing_balance_blocks(self) -> None:
        result = self.gate.evaluate(
            evidence(account=AccountEvidence(can_trade=True, available_balance=None, available_balance_captured_at=None))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.AVAILABLE_BALANCE_UNKNOWN,))

    def test_balance_without_captured_at_blocks(self) -> None:
        result = self.gate.evaluate(
            evidence(account=AccountEvidence(can_trade=True, available_balance=1.0, available_balance_captured_at=None))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.AVAILABLE_BALANCE_UNKNOWN,))

    def test_stale_balance_blocks(self) -> None:
        result = self.gate.evaluate(
            evidence(account=AccountEvidence(can_trade=True, available_balance=1.0, available_balance_captured_at=NOW - 30_001))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.AVAILABLE_BALANCE_STALE,))

    def test_zero_balance_is_known_not_unknown(self) -> None:
        """0 是已知事实（余额为 0），不是"未知"——原因码必须是别的（这里应通过）。"""
        result = self.gate.evaluate(
            evidence(account=AccountEvidence(can_trade=True, available_balance=0.0, available_balance_captured_at=NOW))
        )

        self.assertIs(result.status, LiveReadinessStatus.LIVE_READY)


class Sc6RiskPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = LiveReadinessGate(policy=readiness_policy())

    def test_missing_policy_blocks(self) -> None:
        result = self.gate.evaluate(evidence(risk_policy=None))

        self.assertEqual(result.reasons, (LiveReadinessReason.RISK_LIMITS_NOT_CONFIGURED,))

    def test_kill_switch_not_operable_blocks(self) -> None:
        for mode in (KillSwitchMode.REDUCE_ONLY, KillSwitchMode.HALT_ALL):
            with self.subTest(mode=mode):
                result = self.gate.evaluate(evidence(risk_policy=risk_policy(kill_switch_mode=mode)))
                self.assertEqual(result.reasons, (LiveReadinessReason.KILL_SWITCH_NOT_OPERABLE,))

    def test_policy_rejects_bad_values(self) -> None:
        for overrides in (
            {"max_position_qty": 0.0},
            {"max_daily_loss": -1.0},
            {"max_drawdown_pct": 1.5},
            {"max_drawdown_pct": 0.0},
            {"max_leverage": 0.5},
            {"max_mark_age_ms": 0},
            {"max_mark_age_ms": 1.5},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ReadinessError):
                    risk_policy(**overrides)

    def test_policy_translates_to_risk_limits(self) -> None:
        limits = risk_policy().to_limits()

        self.assertEqual(limits.max_position_qty, 0.01)
        self.assertEqual(limits.max_daily_loss, 50.0)
        self.assertEqual(limits.max_drawdown_pct, 0.10)
        self.assertEqual(limits.max_open_order_exposure, 500.0)
        self.assertEqual(limits.max_leverage, 3.0)
        self.assertEqual(limits.max_mark_age_ms, 5_000)
        self.assertIs(limits.effective_kill_switch_mode, KillSwitchMode.NORMAL)


class Sc9Sc10EnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = LiveReadinessGate(policy=readiness_policy())

    def test_mainnet_without_validation_is_blocked(self) -> None:
        result = self.gate.evaluate(evidence(environment=environment_evidence(environment=Environment.MAINNET)))

        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)
        self.assertEqual(result.reasons, (LiveReadinessReason.MAINNET_PRIVATE_NOT_VALIDATED,))
        self.assertIsNone(result.scope)

    def test_testnet_ready_is_never_reported_as_mainnet(self) -> None:
        result = self.gate.evaluate(evidence())

        self.assertIs(result.scope, LiveReadinessScope.TESTNET_LIVE_READY)
        self.assertIsNot(result.scope, LiveReadinessScope.MAINNET_LIVE_READY)

    def test_mainnet_with_validation_can_be_ready_with_mainnet_scope(self) -> None:
        result = self.gate.evaluate(
            evidence(environment=environment_evidence(environment=Environment.MAINNET, validated=True))
        )

        self.assertIs(result.status, LiveReadinessStatus.LIVE_READY)
        self.assertIs(result.scope, LiveReadinessScope.MAINNET_LIVE_READY)

    def test_all_reasons_are_collected_once(self) -> None:
        result = self.gate.evaluate(
            evidence(
                recovery_status=RecoveryStatus.BLOCKED,
                private_stream=PrivateStreamEvidence("STOPPED", False, False, None, None),
                account=AccountEvidence(can_trade=False, available_balance=None, available_balance_captured_at=None),
                historical_risk=HistoricalRiskEvidence(False, False, False),
                environment=environment_evidence(environment=Environment.MAINNET),
                market=market_evidence(ready=False),
                risk_policy=None,
            )
        )

        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)
        self.assertEqual(len(result.reasons), len(set(result.reasons)))
        self.assertEqual(
            set(result.reasons),
            {
                LiveReadinessReason.RECOVERY_NOT_READY,
                LiveReadinessReason.PRIVATE_STREAM_NOT_READY,
                LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED,
                LiveReadinessReason.CLOCK_NOT_CALIBRATED,
                LiveReadinessReason.ACCOUNT_CANNOT_TRADE,
                LiveReadinessReason.AVAILABLE_BALANCE_UNKNOWN,
                LiveReadinessReason.HISTORICAL_DAILY_PNL_UNKNOWN,
                LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN,
                LiveReadinessReason.RISK_LIMITS_NOT_CONFIGURED,
                LiveReadinessReason.MAINNET_PRIVATE_NOT_VALIDATED,
                LiveReadinessReason.MARKET_NOT_READY,
            },
        )
        self.assertEqual(len(result.details), len(result.reasons))


class HighWatermarkReasonTest(unittest.TestCase):
    """P0001.9.4.2 §16 / SC-1 / SC-7 / SC-10 / SC-11 / SC-13：durable HWM 的精确原因码。"""

    def setUp(self) -> None:
        self.gate = LiveReadinessGate(policy=readiness_policy())

    def test_sc1_uninitialized_blocks_with_precise_reason(self) -> None:
        result = self.gate.evaluate(
            evidence(high_watermark=HighWatermarkEvidence.uninitialized())
        )

        self.assertIn(LiveReadinessReason.HIGH_WATERMARK_NOT_INITIALIZED, result.reasons)
        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)

    def test_store_failure_blocks(self) -> None:
        result = self.gate.evaluate(
            evidence(high_watermark=HighWatermarkEvidence.store_failed(detail="fsync failed"))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.HIGH_WATERMARK_STORE_FAILED,))

    def test_equity_mismatch_blocks(self) -> None:
        result = self.gate.evaluate(
            evidence(high_watermark=HighWatermarkEvidence.equity_mismatch(detail="local 5000 vs exchange 4800"))
        )

        self.assertEqual(result.reasons, (LiveReadinessReason.EQUITY_MISMATCH,))

    def test_capital_flow_blocks_with_dedicated_reason(self) -> None:
        """SC-13：activation 后的 `TRANSFER` ⇒ INVALIDATED ⇒ EXTERNAL_CAPITAL_FLOW_DETECTED。"""
        tracker, store = activated_tracker(equity=1_000.0)
        tracker.invalidate_for_capital_flow(reason="TRANSFER detected", ts=BASE_TS + 1)

        result = self.gate.evaluate(evidence(high_watermark=tracker.evidence()))

        self.assertEqual(result.reasons, (LiveReadinessReason.EXTERNAL_CAPITAL_FLOW_DETECTED,))
        self.assertEqual(store.save_count, 2)  # activate + invalidate 都已持久化

    def test_other_invalidation_blocks_as_invalid(self) -> None:
        tracker, _store = activated_tracker(equity=1_000.0)
        tracker.invalidate_for_capital_flow(
            reason="operator reset without rebase", ts=BASE_TS + 1, kind=HighWatermarkInvalidation.OTHER
        )

        result = self.gate.evaluate(evidence(high_watermark=tracker.evidence()))

        self.assertEqual(result.reasons, (LiveReadinessReason.HIGH_WATERMARK_INVALID,))

    def test_active_high_watermark_does_not_add_reasons(self) -> None:
        result = self.gate.evaluate(evidence())

        self.assertNotIn(LiveReadinessReason.HIGH_WATERMARK_NOT_INITIALIZED, result.reasons)
        self.assertNotIn(LiveReadinessReason.HIGH_WATERMARK_INVALID, result.reasons)
        self.assertIs(result.status, LiveReadinessStatus.LIVE_READY)

    def test_invalidated_status_without_problem_is_still_blocked(self) -> None:
        """防御：状态已是 INVALIDATED 时不因 problem 缺失而放行。"""
        forged = HighWatermarkEvidence(
            status=HighWatermarkStatus.INVALIDATED,
            peak_equity=None,
            peak_ts=None,
            activation_id="act-1",
            activation_ts=BASE_TS,
            activation_equity=1_000.0,
            generation=1,
            scope=HighWatermarkScope.TESTNET,
            detail="",
        )

        result = self.gate.evaluate(evidence(high_watermark=forged))

        self.assertIn(LiveReadinessReason.HIGH_WATERMARK_INVALID, result.reasons)

    def test_high_watermark_must_be_typed(self) -> None:
        with self.assertRaises(ReadinessError):
            evidence(high_watermark="active")  # type: ignore[arg-type]


class Sc11PurityTest(unittest.TestCase):
    """SC-11：readiness 包不得具备任何下单/撤单能力，也不得触碰网络或执行层。"""

    READINESS_DIR = PROJECT_ROOT / "readiness"
    FORBIDDEN_MARKERS = ("submit", "cancel", "/fapi/v1/order", "placeOrder", "socket", "urllib", "requests")

    def test_no_execution_or_network_capability(self) -> None:
        for path in sorted(self.READINESS_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for marker in self.FORBIDDEN_MARKERS:
                with self.subTest(module=path.name, marker=marker):
                    self.assertNotIn(marker, source)

    def test_imports_only_allowed_roots(self) -> None:
        # hashlib / json：canonical evidence digest（P0001.9.5 §9）所需；仍禁止一切网络与执行能力
        allowed = {
            "__future__", "collections", "dataclasses", "enum", "hashlib", "json", "math",
            "market", "risk", "connectors", "readiness",
        }
        for path in sorted(self.READINESS_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots.add(node.module.split(".")[0])
            with self.subTest(module=path.name):
                self.assertEqual(roots - allowed, set())


if __name__ == "__main__":
    unittest.main()
