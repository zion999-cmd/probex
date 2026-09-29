"""把既有事实映射成 readiness 证据（P0001.9.4 §3 / §7）。

这里只做**纯映射**：不触网、不推断业务值、不把未知写成 0。
放进单独模块是为了让 live runner、测试与未来的 orchestration 共用同一套映射，避免各处重复解释。
"""

from __future__ import annotations

from market.events.types import Milliseconds

from risk.history import BASELINE_SOURCE_BINANCE_INCOME, HistoricalRiskBaseline
from risk.types import ExchangeAvailableBalance, RiskSnapshot

from connectors.binance.private.income import IncomeClass, IncomeHistoryFacts

from connectors.binance.private.account import AccountSnapshotObservation
from connectors.binance.private.telemetry import PrivateStreamTelemetry
from readiness.types import (
    AccountEvidence,
    Environment,
    EnvironmentEvidence,
    EnvironmentValidationEvidence,
    EnvironmentValidationStatus,
    HistoricalRiskEvidence,
    PrivateStreamEvidence,
    ReadinessError,
)


def private_stream_evidence(
    telemetry: PrivateStreamTelemetry, *, boundary_present: bool
) -> PrivateStreamEvidence:
    """由 private runtime telemetry 构造证据。

    telemetry 本身不含 boundary 字段（它由 `PrivateAccountRuntime.snapshot_boundary` 表达），
    因此 `boundary_present` 必须显式传入——不允许默认成 True（未知 ≠ 满足）。
    """
    if not isinstance(telemetry, PrivateStreamTelemetry):
        raise ReadinessError("private_stream_evidence requires a PrivateStreamTelemetry")
    if not isinstance(boundary_present, bool):
        raise ReadinessError("boundary_present must be a bool")
    distribution = telemetry.private_lag_ms
    return PrivateStreamEvidence(
        listen_key_state=telemetry.listen_key_state,
        continuity_assumed=telemetry.continuity_assumed,
        boundary_present=boundary_present,
        median_private_lag_ms=None if distribution is None else distribution.median,
        clock_calibration=telemetry.clock_calibration,
        # P0001.9.7.1：区分"从未有事件"（UNOBSERVED）与"有事件但无法测量"（UNKNOWN）
        measured_lag_samples=0 if distribution is None else int(distribution.samples),
        unmeasured_lag_samples=int(telemetry.uncorrected_lag_sample_count),
    )


def account_evidence(observation: AccountSnapshotObservation) -> AccountEvidence:
    """由交易所账户快照构造证据；余额缺失保持 `None`（未知 ≠ 0）。"""
    if not isinstance(observation, AccountSnapshotObservation):
        raise ReadinessError("account_evidence requires an AccountSnapshotObservation")
    settlement = observation.settlement_balance
    return AccountEvidence(
        can_trade=observation.can_trade,
        available_balance=None if settlement is None else settlement.available_balance,
        available_balance_captured_at=None if settlement is None else observation.receive_ts,
    )


def exchange_available_balance(
    observation: AccountSnapshotObservation,
) -> ExchangeAvailableBalance | None:
    """交易所权威可用余额（Binance `availableBalance`）；缺失返回 None（绝不回退成本地估算）。"""
    if not isinstance(observation, AccountSnapshotObservation):
        raise ReadinessError("exchange_available_balance requires an AccountSnapshotObservation")
    settlement = observation.settlement_balance
    if settlement is None:
        return None
    return ExchangeAvailableBalance(value=settlement.available_balance, captured_at=observation.receive_ts)


def historical_risk_evidence_from_snapshot(snapshot: RiskSnapshot) -> HistoricalRiskEvidence:
    """由 `RiskSnapshot` 的"已知性"字段推导历史风险证据（单一来源，不重复解释）。

    `None` = 未知（不是 0）⇒ readiness 必须据此 BLOCKED，而不是把未知当 0。
    """
    if not isinstance(snapshot, RiskSnapshot):
        raise ReadinessError("historical_risk_evidence_from_snapshot requires a RiskSnapshot")
    return HistoricalRiskEvidence(
        daily_pnl_known=snapshot.realized_pnl_today is not None,
        drawdown_known=snapshot.drawdown is not None,
        peak_equity_known=snapshot.peak_equity is not None,
    )


def historical_risk_evidence(
    *, daily_pnl_known: bool, drawdown_known: bool, peak_equity_known: bool
) -> HistoricalRiskEvidence:
    """历史风险"已知性"必须由调用方显式给出（本阶段不做历史重建）。"""
    for name, value in (
        ("daily_pnl_known", daily_pnl_known),
        ("drawdown_known", drawdown_known),
        ("peak_equity_known", peak_equity_known),
    ):
        if not isinstance(value, bool):
            raise ReadinessError(f"historical_risk_evidence: {name} must be a bool")
    return HistoricalRiskEvidence(
        daily_pnl_known=daily_pnl_known,
        drawdown_known=drawdown_known,
        peak_equity_known=peak_equity_known,
    )


def historical_risk_baseline_from_income(
    facts: IncomeHistoryFacts, *, day_start_ts: Milliseconds, cutoff_ts: Milliseconds
) -> HistoricalRiskBaseline:
    """把 Binance income 事实映射成**领域** baseline（P0001.9.4.1 §8）。

    只有 ① 覆盖完整、② 无未分类记录、③ 窗口/边界自洽 时 `daily_pnl_known` 才为 True；
    否则 `daily_net_realized=None`（未知 ≠ 0）。`drawdown_known` / `peak_equity_known` 永远为 False。
    """
    if not isinstance(facts, IncomeHistoryFacts):
        raise ReadinessError("historical_risk_baseline_from_income requires IncomeHistoryFacts")
    problems: list[str] = []
    if not facts.coverage.complete:
        problems.append(f"income history incomplete: {facts.coverage.reason or 'unknown reason'}")
    if facts.unclassified_rows:
        types = sorted({row.income_type for row in facts.unclassified_rows})
        problems.append(f"UNCLASSIFIED_INCOME: {types}")
    if facts.coverage.start_ts < day_start_ts:
        problems.append("coverage starts before the requested UTC day start")
    if facts.coverage.end_ts > cutoff_ts:
        problems.append("coverage ends after cutoff_ts")
    return HistoricalRiskBaseline(
        day_start_ts=day_start_ts,
        cutoff_ts=cutoff_ts,
        daily_net_realized=None if problems else facts.trading_net_realized,
        trading_rows=len(facts.by_class(IncomeClass.TRADING)),
        non_trading_rows=len(facts.by_class(IncomeClass.NON_TRADING)),
        coverage_complete=facts.coverage.complete,
        source=BASELINE_SOURCE_BINANCE_INCOME,
        detail="; ".join(problems),
    )


def environment_evidence(
    *,
    environment: Environment,
    validation_status: EnvironmentValidationStatus = EnvironmentValidationStatus.NOT_VALIDATED,
    validated_at: Milliseconds | None = None,
    validation_id: str | None = None,
    account_scope: str | None = None,
    evidence_source: str | None = None,
) -> EnvironmentEvidence:
    """**typed** 环境验收证据（P0001.9.5 §4：不再接受裸 `mainnet_private_validated=True`）。

    `MAINNET_LIVE_READY` 需要 `validation_status == VALIDATED` 且 `validation_id` / `account_scope` /
    `evidence_source` / `validated_at` 齐备（由 `EnvironmentValidationEvidence.mainnet_private_validated` 判定）。
    """
    if not isinstance(environment, Environment):
        raise ReadinessError("environment_evidence requires an Environment")
    if not isinstance(validation_status, EnvironmentValidationStatus):
        raise ReadinessError("environment_evidence requires an EnvironmentValidationStatus")
    return EnvironmentEvidence(
        environment=environment,
        validation=EnvironmentValidationEvidence(
            environment=environment,
            validation_status=validation_status,
            validated_at=validated_at,
            validation_id=validation_id,
            account_scope=account_scope,
            evidence_source=evidence_source,
        ),
    )


def calibration_age_ms(calibration, *, now_ms: Milliseconds) -> int:
    """校准新鲜度（供报告使用）。"""
    if calibration is None:
        raise ReadinessError("calibration_age_ms requires a ClockCalibration")
    return calibration.age_ms(now_ms=now_ms)


__all__ = [
    "account_evidence",
    "calibration_age_ms",
    "environment_evidence",
    "exchange_available_balance",
    "historical_risk_baseline_from_income",
    "historical_risk_evidence",
    "historical_risk_evidence_from_snapshot",
    "private_stream_evidence",
]
