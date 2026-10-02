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


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
                replay_position: int | None = None, timestamp: object | None = None,
                timeframe: object | None = None, candle: object | None = None,
                drawing: object | None = None) -> AssistantContext:
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
            selected_timestamp=(Fact.unknown("no chart selection") if timestamp in (None, "")
                                else Fact.of(_coerce_int(timestamp))),
            timeframe=(Fact.unknown("no chart selection") if not timeframe else Fact.of(str(timeframe))),
            selected_candle=(Fact.unknown("no candle selected") if not candle else Fact.of(str(candle))),
            selected_drawing=(Fact.unknown("no drawing selected") if not drawing else Fact.of(str(drawing))),
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

    # ------------------------------------------------------------------ ops explain (F-12/F-15)

    def explain_ops(self, topic: str) -> dict[str, object]:
        """解释 operational posture（只读；不新增写型 action）。

        topic：`network_exposure` / `auth_required` / `retention_unbounded` /
        `process_live_readiness_blocked`。
        """
        from product.reason_catalog import explain_reason

        snapshot = self.snapshot_provider()
        ops = snapshot.ops
        network = ops.network.value if ops.network.known and isinstance(ops.network.value, dict) else {}
        retention = (ops.retention.value if ops.retention.known and isinstance(ops.retention.value, dict)
                     else {})
        topics: dict[str, tuple[str, dict[str, object]]] = {
            "network_exposure": ("NON_LOOPBACK_NOT_ALLOWED", {
                "bind_host": network.get("bind_host"), "loopback": network.get("loopback"),
                "allow_non_loopback": network.get("allow_non_loopback")}),
            "auth_required": ("AUTH_TOKEN_REQUIRED", {
                "auth_required": network.get("auth_required"),
                "auth_token_ref": network.get("auth_token_ref"),
                "scheme": network.get("scheme")}),
            "retention_unbounded": ("RETENTION_UNBOUNDED", {
                "policy": retention.get("policy"), "bounded": retention.get("bounded")}),
            "process_live_readiness_blocked": ("PROCESS_LIVE_READINESS_BLOCKED", {
                "process_live": ops.process_live.value if ops.process_live.known else None,
                "runtime_state": ops.runtime_state.value if ops.runtime_state.known else None,
                "trade_readiness": ops.trade_readiness.value if ops.trade_readiness.known else None,
                "trade_readiness_reasons": list(ops.trade_readiness_reasons),
                "execution_health": (ops.execution_health.value if ops.execution_health.known else None),
                "operational_warning": (ops.operational_warning.value
                                        if ops.operational_warning.known else None)}),
        }
        if topic not in topics:
            raise ValueError(f"unknown ops topic {topic!r} (allowed: {sorted(topics)})")
        reason_code, evidence = topics[topic]
        return {"topic": topic, "reason_code": reason_code,
                "explanation": explain_reason(reason_code).to_payload(), "evidence": evidence}

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
