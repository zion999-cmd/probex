"""每轮编排 telemetry（P0001.9.7 §16）。

目标：能回答"为什么这一轮下了 / 没下 / 撤了 / 没撤"。只记录**事实与决策**，不含凭据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from market.events.types import Milliseconds


@dataclass(frozen=True, slots=True)
class LoopTelemetry:
    """一轮 loop 的完整事实（immutable；便于落盘与断言）。"""

    loop_ts: Milliseconds
    #: 本轮输入的 market state（无 ⇒ None）
    market_state_id: str | None
    market_state_hash: str | None
    market_healthy: bool
    #: 本轮使用的 prediction（无 ⇒ None）
    prediction_id: str | None
    prediction_age_ms: Milliseconds | None
    #: 账户与暴露
    position_qty: float
    open_order_exposure: float
    uncertain_exposure: float
    #: authority 事实
    authority_id: str | None
    authority_valid: bool
    authority_reason: str | None
    #: 策略决策
    maker_decision: str
    bid_action: str
    ask_action: str
    blocked_by: str | None
    #: 本轮动作计数
    submit_count: int = 0
    cancel_count: int = 0
    replace_count: int = 0
    risk_reject_count: int = 0
    #: 失败/收敛相关
    unknown_submit_count: int = 0
    unknown_cancel_count: int = 0
    skipped_transition_count: int = 0
    reconciliation_triggered: bool = False
    reconciliation_converged: bool | None = None
    #: 执行是否被禁用（observe-only）
    execution_disabled: bool = True
    #: 人可读说明（"为什么"）
    notes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        return {
            "loop_ts": self.loop_ts,
            "market_state_id": self.market_state_id,
            "market_state_hash": self.market_state_hash,
            "market_healthy": self.market_healthy,
            "prediction_id": self.prediction_id,
            "prediction_age_ms": self.prediction_age_ms,
            "position_qty": self.position_qty,
            "open_order_exposure": self.open_order_exposure,
            "uncertain_exposure": self.uncertain_exposure,
            "authority_id": self.authority_id,
            "authority_valid": self.authority_valid,
            "authority_reason": self.authority_reason,
            "maker_decision": self.maker_decision,
            "bid_action": self.bid_action,
            "ask_action": self.ask_action,
            "blocked_by": self.blocked_by,
            "submit_count": self.submit_count,
            "cancel_count": self.cancel_count,
            "replace_count": self.replace_count,
            "risk_reject_count": self.risk_reject_count,
            "unknown_submit_count": self.unknown_submit_count,
            "unknown_cancel_count": self.unknown_cancel_count,
            "skipped_transition_count": self.skipped_transition_count,
            "reconciliation_triggered": self.reconciliation_triggered,
            "reconciliation_converged": self.reconciliation_converged,
            "execution_disabled": self.execution_disabled,
            "notes": list(self.notes),
        }


@dataclass
class LoopTelemetrySink:
    """有界内存 sink（不引入任何外部存储/数据库）。"""

    limit: int = 2_000
    records: list[LoopTelemetry] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError("LoopTelemetrySink.limit must be an int >= 1")

    def record(self, telemetry: LoopTelemetry) -> None:
        self.records.append(telemetry)
        if len(self.records) > self.limit:
            del self.records[0 : len(self.records) - self.limit]

    def latest(self) -> LoopTelemetry | None:
        return self.records[-1] if self.records else None

    def window(self, *, count: int) -> tuple[LoopTelemetry, ...]:
        if count < 1:
            raise ValueError("count must be >= 1")
        return tuple(self.records[-count:])

    @property
    def totals(self) -> dict[str, int]:
        """累计计数（验收用；全部是**动作事实**）。"""
        fields = (
            "submit_count",
            "cancel_count",
            "replace_count",
            "risk_reject_count",
            "unknown_submit_count",
            "unknown_cancel_count",
            "skipped_transition_count",
        )
        return {name: sum(getattr(entry, name) for entry in self.records) for name in fields}

    @property
    def reconciliation_runs(self) -> int:
        return sum(1 for entry in self.records if entry.reconciliation_triggered)


__all__ = ["LoopTelemetry", "LoopTelemetrySink"]
