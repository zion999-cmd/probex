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


def _as_fact(value: object, reason: str) -> Fact:
    """已经是 `Fact` 的字段直接透传（避免 `Fact.of(Fact)` 造成不可序列化）。"""
    if isinstance(value, Fact):
        return value
    return Fact.of(value, unknown_reason=reason)


def _instrument_facts(snapshot: object) -> dict[str, Fact]:
    """P0001.15 §27：把 instrument / venue / reference price / connector 事实搬进 assistant 上下文。"""
    instrument = getattr(snapshot, "instrument", None)
    venue = getattr(snapshot, "venue", None)
    reference = getattr(snapshot, "reference_price", None)
    market_connector = getattr(snapshot, "market_connector_health", None)
    private_connector = getattr(snapshot, "private_connector_health", None)
    if instrument is None or venue is None or reference is None:
        return {}
    return {
        "instrument_id": instrument.instrument_id,
        "asset_class": instrument.asset_class,
        "product_type": instrument.product_type,
        "instrument_capabilities": instrument.capabilities,
        "venue_id": venue.venue_id,
        "venue_environment": venue.environment,
        "market_connector": _as_fact(getattr(market_connector, "connector_id", None),
                                     "no market connector"),
        "execution_connector": _as_fact(getattr(private_connector, "connector_id", None),
                                        "no execution connector"),
        "reference_price": reference.price,
        "reference_price_type": reference.price_type,
        "reference_price_source": reference.source,
        "reference_price_reason": reference.reason,
    }


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
            **_instrument_facts(snapshot),
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
            "answers": self._instrument_answers(snapshot, kind=kind, identity=identity),
            "note": "explanation is composed from existing product facts (no LLM, no new truth)",
        }

    # ------------------------------------------------------------------ instrument / venue Q&A

    def _instrument_answers(self, snapshot: object, *, kind: str, identity: str) -> dict[str, str]:
        """P0001.15 §27 的确定性问答（只组合既有事实；不推断、不调用 LLM）。

        覆盖人类裁决第 6 条要求 Assistant 能回答的五个问题。
        """
        instrument = getattr(snapshot, "instrument", None)
        venue = getattr(snapshot, "venue", None)
        reference = getattr(snapshot, "reference_price", None)
        answers: dict[str, str] = {}
        if instrument is not None and instrument.product_type.known:
            product = str(instrument.product_type.value)
            asset = str(instrument.asset_class.value)
            symbol = str(instrument.symbol.value)
            spot_or_perp = ("SPOT（现货，不持有合约仓位）" if product == "SPOT"
                            else f"{product}（{asset}）")
            answers["spot_or_perpetual"] = (
                f"{symbol} 是 {spot_or_perp}；instrument_id={instrument.instrument_id.value}，"
                f"结算资产={instrument.settlement_asset.value}")
            capabilities = instrument.capabilities.value if instrument.capabilities.known else {}
            supports_short = capabilities.get("supports_short")
            supports_reduce_only = capabilities.get("supports_reduce_only")
            has_funding = capabilities.get("has_funding")
            answers["why_short_and_reduce_only"] = (
                f"supports_short={supports_short}，supports_reduce_only={supports_reduce_only}，"
                f"has_funding={has_funding}（product semantics，由 instrument domain 声明，"
                "不由 venue 名称推断）")
        if venue is not None and venue.venue_id.known:
            answers["venue_and_connectors"] = (
                f"venue_id={venue.venue_id.value}（environment={venue.environment.value}）；"
                f"market connector={venue.market_connector_id.value}；"
                f"execution connector={venue.execution_connector_id.value}（两者 health 独立）")
        if reference is not None:
            if reference.known.known and reference.known.value:
                answers["reference_price_source"] = (
                    f"当前 reference price 来自 {reference.source.value}"
                    f"（price_type={reference.price_type.value}，price={reference.price.value}，"
                    f"as_of={reference.as_of.value}，freshness_ms={reference.freshness_ms.value}）")
            else:
                reason = reference.reason.value if reference.reason.known else "UNKNOWN"
                answers["reference_price_source"] = (
                    f"reference price 未知（reason={reason}）：正式 MARK 来源未提供事实 ⇒ 风险事实缺失，"
                    "RiskGate 按既有规则 fail closed（新增暴露被拒绝）；"
                    "本系统不会用 last trade / mid 冒充 mark price")
        if kind in ("order", "decision") and identity:
            answers["order_decision_link"] = self._order_decision_answer(snapshot, kind=kind,
                                                                        identity=identity)
        return answers

    def _order_decision_answer(self, snapshot: object, *, kind: str, identity: str) -> str:
        execution = getattr(snapshot, "execution", None)
        orders = tuple(getattr(execution, "active_orders", ()) or ())
        for order in orders:
            if kind == "order" and order.client_order_id == identity:
                decided = order.decision_id.value if order.decision_id.known else "UNKNOWN"
                return (f"order {order.client_order_id} 来自 decision_id={decided}"
                        f"（instrument={order.instrument_id.value if order.instrument_id.known else 'UNKNOWN'}，"
                        f"venue={order.venue_id.value if order.venue_id.known else 'UNKNOWN'}）；"
                        "关联来自订单自身的 canonical correlation metadata")
            if kind == "decision" and order.decision_id.known and order.decision_id.value == identity:
                return (f"decision_id={identity} 产生了 order {order.client_order_id}"
                        "（由 OrderTracker 的 canonical correlation 反查）")
        return f"未在 active orders 中找到与 {kind}={identity} 关联的订单（可能已终态或未提交）"


__all__ = ["AssistantService"]
