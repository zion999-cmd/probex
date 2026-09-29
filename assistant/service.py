"""Assistant Service（P0001.12.3）：上下文 + 建议动作 + 解释（全部来自既有事实与 Manifest）。

第一版**不调用任何 LLM**：`explain` 是确定性的证据组合（evidence + raw facts + blockers + metrics），
因此不引入 provider 凭据、不引入不透明推理。建议动作只能来自 Action Manifest。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from actions.gateway import ActionContext, ActionGateway
from assistant.context import AssistantContext
from product.snapshot import SystemSnapshot
from product.types import Fact, RuntimeMode


@dataclass(slots=True)
class AssistantService:
    """产品助手（read-only + manifest 建议；不拥有交易能力）。"""

    snapshot_provider: Callable[[], SystemSnapshot]
    gateway: ActionGateway
    raw_facts_provider: Callable[[str, str], object] | None = None
    evidence_provider: Callable[[], object | None] = None

    # ------------------------------------------------------------------ context

    def context(self, *, surface: str = "monitor", selected: Mapping[str, str] | None = None,
                replay_position: int | None = None) -> AssistantContext:
        snapshot = self.snapshot_provider()
        selection = dict(selected or {})
        return AssistantContext(
            surface=surface,
            runtime=snapshot.runtime,
            selected_run_id=Fact.of(selection.get("run")),
            selected_decision_id=Fact.of(selection.get("decision")),
            selected_order_id=Fact.of(selection.get("order")),
            selected_fill_id=Fact.of(selection.get("fill")),
            replay_position=(Fact.unknown("replay position not reported")
                             if replay_position is None else Fact.of(int(replay_position))),
            active_blockers=tuple(f"{b.owner.value}:{b.reason_code}" for b in snapshot.blockers),
            selection=selection,
        )

    def action_context(self, context: AssistantContext) -> ActionContext:
        return ActionContext(runtime_id=context.runtime.runtime_id, mode=context.runtime.mode,
                             environment=context.runtime.environment, venue=context.runtime.venue,
                             symbol=context.runtime.symbol, surface=context.surface,
                             selected=dict(context.selection))

    # ------------------------------------------------------------------ suggestions

    def suggested_actions(self, context: AssistantContext) -> tuple[dict[str, object], ...]:
        """建议动作**只能**来自 Manifest（AI 不得自创 API）。"""
        manifest = self.gateway.manifest()
        mode = context.runtime.mode
        suggestions = []
        for entry in manifest:
            if not entry["available"]:
                continue
            if mode.value not in entry["allowed_modes"]:
                continue
            suggestions.append({"action_id": entry["action_id"], "level": entry["level"],
                                "confirmation_required": entry["confirmation_required"]})
        return tuple(suggestions)

    # ------------------------------------------------------------------ explain

    def explain(self, kind: str, identity: str) -> dict[str, object]:
        """确定性解释：把既有事实与因果链组合起来（不调用 LLM、不推断业务语义）。"""
        snapshot = self.snapshot_provider()
        trace = [entry for entry in snapshot.evidence.trace
                 if identity in (entry.identity.value if entry.identity.known else "")]
        facts: dict[str, object] = {}
        if self.raw_facts_provider is not None:
            view = self.raw_facts_provider(kind, identity)
            facts = {"available": bool(getattr(view, "available", False)),
                     "names": [name for name, _ in getattr(view, "facts", ())]}
        return {
            "kind": kind,
            "identity": identity,
            "trace": [
                {"stage": entry.stage, "outcome": entry.outcome,
                 "reason_code": entry.reason_code.value if entry.reason_code.known else None,
                 "detail": entry.detail}
                for entry in trace
            ],
            "raw_facts": facts,
            "blockers": [f"{b.owner.value}:{b.reason_code}" for b in snapshot.blockers],
            "note": "explanation is composed from existing product facts (no LLM, no new truth)",
        }


__all__ = ["AssistantService"]
