"""Execution Safety 类型（P0001.13）：全部为**只读事实**，不改变任何交易语义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from market.events.types import Milliseconds

from product.types import Fact

#: 延迟五阶段（SC-6：定义稳定）
LATENCY_STAGES: tuple[str, ...] = (
    "decision_to_submit",
    "submit_to_ack",
    "cancel_to_ack",
    "event_receive_lag",
    "reconciliation_duration",
)

#: ExecutionHealth 的输入来源（SC-8：可解释降级来源）
HEALTH_SOURCES: tuple[str, ...] = (
    "rate_limit_state",
    "venue_facts_state",
    "latency_state",
    "private_latency",
    "uncertain_exposure",
    "unknown_lost_orders",
    "reconciliation_state",
)


class RateLimitStatus(Enum):
    HEALTHY = "HEALTHY"
    NEAR_LIMIT = "NEAR_LIMIT"
    EXHAUSTED = "EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class ExecutionHealthStatus(Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class Severity(Enum):
    """policy 中每来源的严重度（与 BlockerView 三档一致）。"""

    BLOCKING = "BLOCKING"
    DEGRADED = "DEGRADED"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class VenueLimitState:
    """交易所限额事实（价格/数量来自 `TradingRules`；rate/order 限额必须有真实证据）。"""

    tick_size: Fact
    step_size: Fact
    min_qty: Fact
    max_qty: Fact
    min_notional: Fact
    max_orders: Fact
    request_weight_limit: Fact
    order_rate_limit: Fact
    source: str
    observed_at: Fact
    age_ms: Fact

    def price_qty_facts(self) -> dict[str, Fact]:
        return {"tick_size": self.tick_size, "step_size": self.step_size, "min_qty": self.min_qty,
                "max_qty": self.max_qty, "min_notional": self.min_notional}


@dataclass(frozen=True, slots=True)
class BudgetState:
    """一条 budget 的当前状态（unknown ⇒ 所有数值保持 UNKNOWN，不填 0）。"""

    kind: str
    status: RateLimitStatus
    limit: Fact
    used: Fact
    remaining: Fact
    reset_at: Fact
    source: str
    window_ms: Fact


@dataclass(frozen=True, slots=True)
class GovernorState:
    """request / order 两条 budget 的合计状态。"""

    request: BudgetState
    order: BudgetState
    allows_new_exposure: bool
    allows_de_risking: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LatencyStats:
    stage: str
    status: str
    p50: Fact
    p95: Fact
    maximum: Fact
    sample_count: int
    budget_ms: Fact


@dataclass(frozen=True, slots=True)
class LatencyState:
    stages: tuple[LatencyStats, ...]
    window_ms: Milliseconds
    min_samples: int

    def stage(self, name: str) -> LatencyStats | None:
        return next((item for item in self.stages if item.stage == name), None)


@dataclass(frozen=True, slots=True)
class ReconciliationState:
    state: Fact
    last_run_at: Fact
    reason: Fact
    actions: Fact
    converged: Fact
    unknown_count: Fact
    lost_count: Fact
    required: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionHealth:
    status: ExecutionHealthStatus
    reasons: tuple[str, ...] = ()
    source_status: dict[str, str] = field(default_factory=dict)


__all__ = [
    "BudgetState",
    "ExecutionHealth",
    "ExecutionHealthStatus",
    "GovernorState",
    "HEALTH_SOURCES",
    "LATENCY_STAGES",
    "LatencyState",
    "LatencyStats",
    "RateLimitStatus",
    "ReconciliationState",
    "Severity",
    "VenueLimitState",
]
