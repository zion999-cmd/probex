"""Live Readiness 契约类型（P0001.9.4 §1 / §4 / §6 / §7）。

纪律：

- **任何未知不得转成 0 / False-positive READY**：缺失值用 `None` 表达，由 gate fail closed。
- **不设置默认业务阈值**：`ReadinessPolicy` / `LiveRiskPolicy` 全部字段必填、无默认值（数值由人类配置）。
- 本模块不做任何 I/O（readiness 必须是可测试、可审计的纯判定）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from market.events.types import Milliseconds
from risk.limits import RiskLimits
from risk.types import KillSwitchMode

from connectors.binance.private.auth import ClockCalibration
from connectors.binance.private.recovery import RecoveryStatus


class ReadinessError(Exception):
    """Readiness 领域错误基类（输入契约不合法）。"""


class Environment(Enum):
    """运行环境。**Testnet 证据永远不能推广成 Mainnet 结论**（提案 §7）。"""

    TESTNET = "testnet"
    MAINNET = "mainnet"


class LiveReadinessStatus(Enum):
    NOT_EVALUATED = "not_evaluated"
    LIVE_READY = "live_ready"
    BLOCKED = "blocked"


class LiveReadinessScope(Enum):
    """`LIVE_READY` 的**作用域**：避免把 testnet 的绿色误读成主网许可。"""

    TESTNET_LIVE_READY = "testnet_live_ready"
    MAINNET_LIVE_READY = "mainnet_live_ready"


class LiveReadinessReason(Enum):
    """阻塞原因码（提案 §1 全部列出，另加 `KILL_SWITCH_NOT_OPERABLE`）。"""

    RECOVERY_NOT_READY = "RECOVERY_NOT_READY"
    PRIVATE_STREAM_NOT_READY = "PRIVATE_STREAM_NOT_READY"
    ACCOUNT_CANNOT_TRADE = "ACCOUNT_CANNOT_TRADE"
    AVAILABLE_BALANCE_UNKNOWN = "AVAILABLE_BALANCE_UNKNOWN"
    AVAILABLE_BALANCE_STALE = "AVAILABLE_BALANCE_STALE"
    HISTORICAL_DAILY_PNL_UNKNOWN = "HISTORICAL_DAILY_PNL_UNKNOWN"
    HISTORICAL_DRAWDOWN_UNKNOWN = "HISTORICAL_DRAWDOWN_UNKNOWN"
    RISK_LIMITS_NOT_CONFIGURED = "RISK_LIMITS_NOT_CONFIGURED"
    #: kill switch 不在 NORMAL（无法作为"可用的止损手段"）
    KILL_SWITCH_NOT_OPERABLE = "KILL_SWITCH_NOT_OPERABLE"
    CLOCK_NOT_CALIBRATED = "CLOCK_NOT_CALIBRATED"
    CLOCK_UNCERTAINTY_TOO_HIGH = "CLOCK_UNCERTAINTY_TOO_HIGH"
    PRIVATE_LATENCY_UNKNOWN = "PRIVATE_LATENCY_UNKNOWN"
    PRIVATE_LATENCY_TOO_HIGH = "PRIVATE_LATENCY_TOO_HIGH"
    MAINNET_PRIVATE_NOT_VALIDATED = "MAINNET_PRIVATE_NOT_VALIDATED"
    MARKET_NOT_READY = "MARKET_NOT_READY"


@dataclass(frozen=True, slots=True)
class ReadinessPolicy:
    """readiness 判定阈值（**全部必填**，无默认业务值）。"""

    max_clock_uncertainty_ms: Milliseconds
    max_median_private_lag_ms: Milliseconds
    max_calibration_age_ms: Milliseconds
    max_available_balance_age_ms: Milliseconds

    def __post_init__(self) -> None:
        for name in (
            "max_clock_uncertainty_ms",
            "max_median_private_lag_ms",
            "max_calibration_age_ms",
            "max_available_balance_age_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReadinessError(f"ReadinessPolicy.{name} must be a non-negative int")


@dataclass(frozen=True, slots=True)
class LiveRiskPolicy:
    """真实资金阶段必须**显式配置**的风险策略（提案 §4）。

    `RiskLimits` 允许 `None` 是因为研究环境需要；但真实资金不能把"不检查"当成"安全"。
    这里的字段全部必填，且可以通过 `to_limits()` 直接喂给逐订单的 `RiskGate`（单一来源，不重复维护数值）。
    数值由人类配置；本类不提供任何默认值。
    """

    max_position_qty: float
    max_order_notional: float
    max_open_order_exposure: float
    max_daily_loss: float
    max_drawdown_pct: float
    max_leverage: float
    max_mark_age_ms: Milliseconds
    kill_switch_mode: KillSwitchMode = KillSwitchMode.NORMAL

    def __post_init__(self) -> None:
        for name in ("max_position_qty", "max_order_notional", "max_open_order_exposure", "max_daily_loss"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ReadinessError(f"LiveRiskPolicy.{name} must be a number")
            number = float(value)
            if not math.isfinite(number) or number <= 0.0:
                raise ReadinessError(f"LiveRiskPolicy.{name} must be a positive finite number (got {value!r})")
            object.__setattr__(self, name, number)
        if isinstance(self.max_drawdown_pct, bool) or not isinstance(self.max_drawdown_pct, (int, float)):
            raise ReadinessError("LiveRiskPolicy.max_drawdown_pct must be a number")
        pct = float(self.max_drawdown_pct)
        if not math.isfinite(pct) or pct <= 0.0 or pct > 1.0:
            raise ReadinessError(f"LiveRiskPolicy.max_drawdown_pct must be in (0, 1], got {pct!r}")
        object.__setattr__(self, "max_drawdown_pct", pct)
        if isinstance(self.max_leverage, bool) or not isinstance(self.max_leverage, (int, float)):
            raise ReadinessError("LiveRiskPolicy.max_leverage must be a number")
        leverage = float(self.max_leverage)
        if not math.isfinite(leverage) or leverage < 1.0:
            raise ReadinessError(f"LiveRiskPolicy.max_leverage must be >= 1, got {leverage!r}")
        object.__setattr__(self, "max_leverage", leverage)
        if (
            isinstance(self.max_mark_age_ms, bool)
            or not isinstance(self.max_mark_age_ms, int)
            or self.max_mark_age_ms <= 0
        ):
            raise ReadinessError("LiveRiskPolicy.max_mark_age_ms must be a positive int")
        if not isinstance(self.kill_switch_mode, KillSwitchMode):
            raise ReadinessError("LiveRiskPolicy.kill_switch_mode must be a KillSwitchMode")

    @property
    def kill_switch_operable(self) -> bool:
        """NORMAL = 未处于全局拦截状态；REDUCE_ONLY / HALT_ALL 都意味着不能放新单。"""
        return self.kill_switch_mode is KillSwitchMode.NORMAL

    def to_limits(self) -> RiskLimits:
        """转成逐订单 `RiskGate` 使用的限额（同一组显式数值，不重复维护）。"""
        return RiskLimits(
            max_position_qty=self.max_position_qty,
            max_position_notional=None,
            max_open_order_exposure=self.max_open_order_exposure,
            max_daily_loss=self.max_daily_loss,
            max_drawdown_pct=self.max_drawdown_pct,
            max_leverage=self.max_leverage,
            max_mark_age_ms=self.max_mark_age_ms,
            kill_switch_mode=self.kill_switch_mode,
        )


@dataclass(frozen=True, slots=True)
class PrivateStreamEvidence:
    """private link 的事实（来自 `PrivateAccountRuntime.telemetry`，不伪造）。"""

    listen_key_state: str
    continuity_assumed: bool
    boundary_present: bool
    #: **校正后**的 median 事件延迟；无样本 ⇒ None（未知 ≠ 达标）
    median_private_lag_ms: int | None
    #: 时钟校准事实（含 RTT / 不确定度 / 测量时刻）
    clock_calibration: ClockCalibration | None


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    """账户事实（来自交易所 account snapshot）。"""

    can_trade: bool
    #: 交易所 `availableBalance`；缺失 ⇒ None（未知 ≠ 0）
    available_balance: float | None
    available_balance_captured_at: Milliseconds | None


@dataclass(frozen=True, slots=True)
class HistoricalRiskEvidence:
    """历史风险事实的**已知性**（本阶段不做历史重建，只消费"是否已知"）。"""

    daily_pnl_known: bool
    drawdown_known: bool
    peak_equity_known: bool


@dataclass(frozen=True, slots=True)
class EnvironmentEvidence:
    """环境证据：`mainnet_private_validated` 必须来自真实的主网只读验收事实。"""

    environment: Environment
    mainnet_private_validated: bool = False


@dataclass(frozen=True, slots=True)
class LiveReadinessEvidence:
    """一次 readiness 评估的全部输入（纯数据，便于测试与审计）。"""

    now_ms: Milliseconds
    recovery_status: RecoveryStatus
    private_stream: PrivateStreamEvidence
    account: AccountEvidence
    historical_risk: HistoricalRiskEvidence
    environment: EnvironmentEvidence
    market_ready: bool
    risk_policy: LiveRiskPolicy | None

    def __post_init__(self) -> None:
        if isinstance(self.now_ms, bool) or not isinstance(self.now_ms, int) or self.now_ms < 0:
            raise ReadinessError("LiveReadinessEvidence.now_ms must be a non-negative int")
        if not isinstance(self.recovery_status, RecoveryStatus):
            raise ReadinessError("LiveReadinessEvidence.recovery_status must be a RecoveryStatus")
        if not isinstance(self.market_ready, bool):
            raise ReadinessError("LiveReadinessEvidence.market_ready must be a bool")


@dataclass(frozen=True, slots=True)
class LiveReadinessResult:
    """readiness 判定结果（带完整 reason code，绝不返回裸 bool）。"""

    status: LiveReadinessStatus
    scope: LiveReadinessScope | None
    reasons: tuple[LiveReadinessReason, ...]
    details: tuple[str, ...] = ()

    @property
    def live_ready(self) -> bool:
        return self.status is LiveReadinessStatus.LIVE_READY

    @property
    def blocked(self) -> bool:
        return self.status is LiveReadinessStatus.BLOCKED


__all__ = [
    "AccountEvidence",
    "Environment",
    "EnvironmentEvidence",
    "HistoricalRiskEvidence",
    "LiveReadinessEvidence",
    "LiveReadinessReason",
    "LiveReadinessResult",
    "LiveReadinessScope",
    "LiveReadinessStatus",
    "LiveRiskPolicy",
    "PrivateStreamEvidence",
    "ReadinessError",
    "ReadinessPolicy",
]
