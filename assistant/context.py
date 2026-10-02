"""Assistant Context（P0001.12.3 §1 / P0001.13）：只引用 Product facts，不复制领域状态。"""

from __future__ import annotations

from dataclasses import dataclass, field

from product.types import Fact, RuntimeIdentity


def _unknown(reason: str) -> Fact:
    return Fact.unknown(reason)


@dataclass(frozen=True, slots=True)
class AssistantContext:
    """每次交互的结构化上下文（用户不必重复描述自己在看什么）。"""

    surface: str
    runtime: RuntimeIdentity
    selected_run_id: Fact
    selected_decision_id: Fact
    selected_order_id: Fact
    selected_fill_id: Fact
    replay_position: Fact
    active_blockers: tuple[str, ...] = ()
    selection: dict[str, str] = field(default_factory=dict)
    #: closure slice：图表选中上下文（canonical ts / timeframe / candle / drawing）
    selected_timestamp: Fact = field(default_factory=lambda: _unknown("no chart selection"))
    timeframe: Fact = field(default_factory=lambda: _unknown("no chart selection"))
    selected_candle: Fact = field(default_factory=lambda: _unknown("no chart selection"))
    selected_drawing: Fact = field(default_factory=lambda: _unknown("no drawing selected"))
    #: P0001.13：执行安全事实（未接线 ⇒ UNKNOWN）
    execution_health: Fact = field(default_factory=lambda: _unknown("execution safety not wired"))
    rate_limit_state: Fact = field(default_factory=lambda: _unknown("execution safety not wired"))
    venue_facts_state: Fact = field(default_factory=lambda: _unknown("execution safety not wired"))
    latency_state: Fact = field(default_factory=lambda: _unknown("execution safety not wired"))
    reconciliation_state: Fact = field(default_factory=lambda: _unknown("execution safety not wired"))
    uncertain_exposure: Fact = field(default_factory=lambda: _unknown("execution safety not wired"))

    def as_payload(self) -> dict[str, object]:
        from product.serialization import to_jsonable

        return {
            "surface": self.surface,
            "runtime": to_jsonable(self.runtime),
            "selected_run_id": to_jsonable(self.selected_run_id),
            "selected_decision_id": to_jsonable(self.selected_decision_id),
            "selected_order_id": to_jsonable(self.selected_order_id),
            "selected_fill_id": to_jsonable(self.selected_fill_id),
            "replay_position": to_jsonable(self.replay_position),
            "active_blockers": list(self.active_blockers),
            "selection": dict(self.selection),
            "selected_timestamp": to_jsonable(self.selected_timestamp),
            "timeframe": to_jsonable(self.timeframe),
            "selected_candle": to_jsonable(self.selected_candle),
            "selected_drawing": to_jsonable(self.selected_drawing),
            "execution_health": to_jsonable(self.execution_health),
            "rate_limit_state": to_jsonable(self.rate_limit_state),
            "venue_facts_state": to_jsonable(self.venue_facts_state),
            "latency_state": to_jsonable(self.latency_state),
            "reconciliation_state": to_jsonable(self.reconciliation_state),
            "uncertain_exposure": to_jsonable(self.uncertain_exposure),
        }


__all__ = ["AssistantContext"]
