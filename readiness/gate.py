"""Live Readiness Gate（P0001.9.4 §1 – §8）。

它回答的唯一问题：

    当前 runtime / account / environment，**是否具备进入真实 ExecutionAdapter 阶段的前置条件**？

输出只有 `LIVE_READY` / `BLOCKED`（+ 完整 reason code 列表）。`RECOVERED` 只是其中一个输入。

纪律：

- **纯判定**：不触网、不读全局状态、不下单/撤单（本包没有任何执行能力）；
- **未知 ≠ 达标**：任何缺失值（未测量 / 未验证 / 未知）都 ⇒ `BLOCKED`；
- **收集全部原因**：一次评估给出完整阻塞清单，而不是只报第一个（利于运维定位）；
- **Testnet ≠ Mainnet**：`scope` 明确标注绿色结论的作用域。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from connectors.binance.private.recovery import RecoveryStatus
from risk.high_watermark import HighWatermarkProblem, HighWatermarkStatus
from readiness.types import (
    Environment,
    LiveReadinessEvidence,
    LiveReadinessReason,
    LiveReadinessResult,
    LiveReadinessScope,
    LiveReadinessStatus,
    ReadinessError,
    ReadinessPolicy,
)

#: private stream 必须处于的 listenKey 状态（与 P0001.9.2 的 7 态状态机一致）。
_ACTIVE_LISTEN_KEY_STATE = "ACTIVE"

#: 记录一条阻塞原因（去重理由码、保留可读 detail）。
_Fail = Callable[[LiveReadinessReason, str], None]


@dataclass(frozen=True, slots=True)
class LiveReadinessGate:
    """按 `ReadinessPolicy` 评估 `LiveReadinessEvidence`（无副作用、可重复）。"""

    policy: ReadinessPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ReadinessPolicy):
            raise ReadinessError("LiveReadinessGate.policy must be a ReadinessPolicy")

    def evaluate(self, evidence: LiveReadinessEvidence) -> LiveReadinessResult:
        """评估一次 readiness；返回 `LIVE_READY`（含 scope）或 `BLOCKED`（含全部原因）。"""
        if not isinstance(evidence, LiveReadinessEvidence):
            raise ReadinessError("evaluate() requires a LiveReadinessEvidence")

        reasons: list[LiveReadinessReason] = []
        details: list[str] = []

        def fail(reason: LiveReadinessReason, detail: str) -> None:
            if reason not in reasons:
                reasons.append(reason)
            details.append(f"{reason.value}: {detail}")

        self._check_recovery(evidence, fail)
        self._check_private_stream(evidence, fail)
        self._check_clock(evidence, fail)
        self._check_account(evidence, fail)
        self._check_high_watermark(evidence, fail)
        self._check_historical_risk(evidence, fail)
        self._check_risk_policy(evidence, fail)
        self._check_environment(evidence, fail)
        if not evidence.market_ready:
            fail(LiveReadinessReason.MARKET_NOT_READY, "market data / feature engine is not healthy")

        if reasons:
            return LiveReadinessResult(
                status=LiveReadinessStatus.BLOCKED,
                scope=None,
                reasons=tuple(reasons),
                details=tuple(details),
            )
        return LiveReadinessResult(
            status=LiveReadinessStatus.LIVE_READY,
            scope=self._scope_for(evidence),
            reasons=(),
            details=(),
        )

    # ------------------------------------------------------------------ 单项检查

    def _check_recovery(self, evidence: LiveReadinessEvidence, fail: _Fail) -> None:
        if evidence.recovery_status is not RecoveryStatus.RECOVERED:
            fail(
                LiveReadinessReason.RECOVERY_NOT_READY,
                f"recovery status is {evidence.recovery_status.value}",
            )

    def _check_private_stream(self, evidence: LiveReadinessEvidence, fail: _Fail) -> None:
        stream = evidence.private_stream
        problems: list[str] = []
        if stream.listen_key_state != _ACTIVE_LISTEN_KEY_STATE:
            problems.append(f"listenKey state is {stream.listen_key_state}")
        if not stream.continuity_assumed:
            problems.append("continuity_assumed is False")
        if not stream.boundary_present:
            problems.append("snapshot boundary is missing")
        if problems:
            fail(LiveReadinessReason.PRIVATE_STREAM_NOT_READY, "; ".join(problems))

        median = stream.median_private_lag_ms
        if median is None:
            fail(LiveReadinessReason.PRIVATE_LATENCY_UNKNOWN, "no corrected private lag samples yet")
        elif median > self.policy.max_median_private_lag_ms:
            fail(
                LiveReadinessReason.PRIVATE_LATENCY_TOO_HIGH,
                f"median corrected lag {median} ms > {self.policy.max_median_private_lag_ms} ms",
            )

    def _check_clock(self, evidence: LiveReadinessEvidence, fail: _Fail) -> None:
        calibration = evidence.private_stream.clock_calibration
        if calibration is None:
            fail(LiveReadinessReason.CLOCK_NOT_CALIBRATED, "clock has never been measured")
            return
        age_ms = calibration.age_ms(now_ms=evidence.now_ms)
        if age_ms > self.policy.max_calibration_age_ms:
            fail(
                LiveReadinessReason.CLOCK_NOT_CALIBRATED,
                f"calibration is {age_ms} ms old > {self.policy.max_calibration_age_ms} ms",
            )
        if calibration.uncertainty_ms > self.policy.max_clock_uncertainty_ms:
            fail(
                LiveReadinessReason.CLOCK_UNCERTAINTY_TOO_HIGH,
                f"uncertainty {calibration.uncertainty_ms} ms > {self.policy.max_clock_uncertainty_ms} ms "
                f"(round trip {calibration.round_trip_ms} ms)",
            )

    def _check_account(self, evidence: LiveReadinessEvidence, fail: _Fail) -> None:
        account = evidence.account
        if not account.can_trade:
            fail(LiveReadinessReason.ACCOUNT_CANNOT_TRADE, "exchange reports canTrade = false")
        if account.available_balance is None:
            fail(LiveReadinessReason.AVAILABLE_BALANCE_UNKNOWN, "exchange availableBalance is missing")
            return
        if account.available_balance_captured_at is None:
            fail(
                LiveReadinessReason.AVAILABLE_BALANCE_UNKNOWN,
                "exchange availableBalance has no captured_at (freshness unknown)",
            )
            return
        age_ms = max(0, evidence.now_ms - account.available_balance_captured_at)
        if age_ms > self.policy.max_available_balance_age_ms:
            fail(
                LiveReadinessReason.AVAILABLE_BALANCE_STALE,
                f"availableBalance is {age_ms} ms old > {self.policy.max_available_balance_age_ms} ms",
            )

    def _check_historical_risk(self, evidence: LiveReadinessEvidence, fail: _Fail) -> None:
        history = evidence.historical_risk
        if not history.daily_pnl_known:
            fail(
                LiveReadinessReason.HISTORICAL_DAILY_PNL_UNKNOWN,
                "today's realized PnL is unknown (e.g. after a startup baseline)",
            )
        if not (history.drawdown_known and history.peak_equity_known):
            fail(
                LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN,
                "historical peak equity / drawdown is unknown",
            )

    def _check_high_watermark(self, evidence: LiveReadinessEvidence, fail: _Fail) -> None:
        """P0001.9.4.2 §16：durable HWM 是 drawdown 的唯一可信来源。"""
        hwm = evidence.high_watermark
        problem = hwm.problem
        if problem is HighWatermarkProblem.STORE_FAILED:
            fail(LiveReadinessReason.HIGH_WATERMARK_STORE_FAILED, hwm.detail or "durable store failed")
            return
        if problem is HighWatermarkProblem.EQUITY_MISMATCH:
            fail(LiveReadinessReason.EQUITY_MISMATCH, hwm.detail or "local/exchange equity mismatch")
            return
        if problem is HighWatermarkProblem.CAPITAL_FLOW:
            fail(
                LiveReadinessReason.EXTERNAL_CAPITAL_FLOW_DETECTED,
                hwm.detail or "external capital flow detected; explicit rebase required",
            )
            return
        if problem is HighWatermarkProblem.INVALID_OTHER:
            fail(LiveReadinessReason.HIGH_WATERMARK_INVALID, hwm.detail or "high-watermark invalidated")
            return
        if hwm.status is HighWatermarkStatus.UNINITIALIZED:
            fail(
                LiveReadinessReason.HIGH_WATERMARK_NOT_INITIALIZED,
                hwm.detail or "high-watermark has never been activated",
            )
            return
        if hwm.status is HighWatermarkStatus.INVALIDATED:  # 防御：状态与 problem 不一致时也不放行
            fail(LiveReadinessReason.HIGH_WATERMARK_INVALID, hwm.detail or "high-watermark invalidated")

    def _check_risk_policy(self, evidence: LiveReadinessEvidence, fail: _Fail) -> None:
        policy = evidence.risk_policy
        if policy is None:
            fail(
                LiveReadinessReason.RISK_LIMITS_NOT_CONFIGURED,
                "no explicit LiveRiskPolicy was provided (RiskLimits=None is not production-safe)",
            )
            return
        if not policy.kill_switch_operable:
            fail(
                LiveReadinessReason.KILL_SWITCH_NOT_OPERABLE,
                f"kill switch mode is {policy.kill_switch_mode.value}",
            )

    def _check_environment(self, evidence: LiveReadinessEvidence, fail: _Fail) -> None:
        environment = evidence.environment
        if environment.environment is Environment.MAINNET and not environment.mainnet_private_validated:
            fail(
                LiveReadinessReason.MAINNET_PRIVATE_NOT_VALIDATED,
                "mainnet private link has no recorded read-only validation "
                "(TESTNET_PRIVATE_VALIDATED does not transfer)",
            )

    # ------------------------------------------------------------------ 作用域

    @staticmethod
    def _scope_for(evidence: LiveReadinessEvidence) -> LiveReadinessScope:
        """绿色结论的作用域：testnet 只能得到 `TESTNET_LIVE_READY`。"""
        if evidence.environment.environment is Environment.MAINNET:
            return LiveReadinessScope.MAINNET_LIVE_READY
        return LiveReadinessScope.TESTNET_LIVE_READY


__all__ = ["LiveReadinessGate"]
