"""Assistant Context（P0001.12.3 §1）：只引用 Product facts，不复制领域状态。"""

from __future__ import annotations

from dataclasses import dataclass, field

from product.types import Fact, RuntimeIdentity


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
        }


__all__ = ["AssistantContext"]
