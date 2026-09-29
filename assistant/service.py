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
    #: P0001.13：执行安全投影（只读；未接线 ⇒ 上下文对应字段 UNKNOWN）
    execution_safety: Callable[[], object | None] = lambda: None

    # ------------------------------------------------------------------ context

    def context(self, *, surface: str = "monitor", selected: Mapping[str, str] | None = None,
                replay_position: int | None = None) -> AssistantContext:
        snapshot = self.snapshot_provider()
        selection = dict(selected or {})
        projection = self.execution_safety()
        from product.serialization import to_jsonable

        if projection is None:
            execution_facts: dict[str, Fact] = {}
        else:
            governor = projection.rate_limits()
            execution_facts = {
                "execution_health": Fact.of(projection.health().status.value),
                "rate_limit_state": (Fact.unknown("execution safety policy not configured")
                                     if governor is None
                                     else Fact.of(to_jsonable({"request": governor.request.status.value,
                                                               "order": governor.order.status.value,
                                                               "allows_new_exposure": governor.allows_new_exposure}))),
                "venue_facts_state": Fact.of(to_jsonable(projection.limits().age_ms.__dict__
                                                         if False else
                                                         {"known": projection.limits().age_ms.known,
                                                          "value": projection.limits().age_ms.value,
                                                          "source": projection.limits().source})),
                "latency_state": (Fact.unknown("execution safety policy not configured")
                                  if projection.latency() is None
                                  else Fact.of(to_jsonable({s_.stage: s_.status
                                                            for s_ in projection.latency().stages}))),
                "reconciliation_state": Fact.of(to_jsonable(projection.reconciliation().state.__dict__
                                                            if False else
                                                            {"known": projection.reconciliation().state.known,
                                                             "value": projection.reconciliation().state.value,
                                                             "required": projection.reconciliation().required})),
                "uncertain_exposure": Fact.of(projection.exposure().get("uncertain_exposure")),
            }
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
            **execution_facts,
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
        # 显式规则（不发明阈值）：出现 RECONCILIATION_REQUIRED blocker 时，若 Manifest 允许，
        # 把受控 reconciliation 动作放在最前（它仍需 confirmation，由 Gateway 负责）
        if any(blocker.endswith("RECONCILIATION_REQUIRED") for blocker in context.active_blockers):
            reconciliation = [item for item in suggestions
                              if item["action_id"] == "runtime.request_reconciliation"]
            if reconciliation:
                suggestions = reconciliation + [item for item in suggestions
                                               if item["action_id"] != "runtime.request_reconciliation"]
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
        # F-09：同一 catalog 给 blocker 附加人类解释（原始 reason_code 保留）
        from product.reason_catalog import explain_codes

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
            "blocker_explanations": explain_codes(b.reason_code for b in snapshot.blockers),
            "note": "explanation is composed from existing product facts (no LLM, no new truth)",
        }


__all__ = ["AssistantService"]
