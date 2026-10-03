"""`ProductService`：从既有 Owner 读取事实并组装 `SystemSnapshot`（P0001.10 §1）。

- **只组合、不重算**（SC-4）：本模块不含任何阈值、指标、PnL 或风险数学；
- 所有读取入口都是**注入的 callable**（便于 Replay / Paper / Testnet 共用）；
- 任何读不到的字段 ⇒ `Fact.unknown(...)`（SC-3：UNKNOWN 不降级为 0 / 空 / healthy）；
- 本模块**不得** import 任何 Binance execution REST client（SC-11）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from market.events.types import Milliseconds

from product.blockers import project_blockers
from product.snapshot import SystemSnapshot
from product.types import (
    UNKNOWN_NOT_AVAILABLE,
    UNKNOWN_NOT_PROVIDED,
    BlockerView,
    FillView,
    ConfigEntryView,
    ConfigView,
    ExecutionSafetyView,
    EvidenceView,
    ExecutionView,
    Fact,
    HealthView,
    MarketView,
    OpsView,
    OrderView,
    PortfolioView,
    PredictionView,
    ReadinessView,
    RiskView,
    RuntimeIdentity,
    StrategyView,
    TraceEntry,
)


def _maker_reason(decision: object | None) -> object | None:
    """maker decision 的**真实**主导原因：`blocked_by` 优先，否则取"当前未报价那一侧"的 trigger。

    P0001.14：`RISK_BUDGET_UNKNOWN` 这类原因由 policy 记在侧向 trigger 上（`blocked_by` 保持 None），
    trace/UI 必须能看到真实原因，而不是退化成"没有原因"。
    """
    if decision is None:
        return None
    blocked = _get(decision, "blocked_by")
    if blocked is not None:
        return _reason_code(blocked)
    for name in ("bid", "ask"):
        side = _get(decision, name)
        action = _get(side, "action")
        if getattr(action, "value", action) != "none":
            continue
        trigger = _get(side, "trigger")
        if trigger is not None:
            return _reason_code(trigger)
    return None


def _reason_code(value: object) -> str:
    """reason code 统一为大写（与 catalog / RiskReasonCode 的既有约定一致）。"""
    return str(getattr(value, "value", value)).upper()


def _get(obj: object | None, name: str) -> object | None:
    """从 Owner 事实对象上取字段；缺失/None ⇒ None（由调用方转成 UNKNOWN）。"""
    if obj is None:
        return None
    return getattr(obj, name, None)


@dataclass(slots=True)
class ProductService:
    """产品读模型服务。所有依赖都是**只读** callable，不持有可变交易状态。"""

    identity: RuntimeIdentity
    market_state: Callable[[], object | None] = lambda: None
    prediction: Callable[[], object | None] = lambda: None
    maker_decision: Callable[[], object | None] = lambda: None
    risk_snapshot: Callable[[], object | None] = lambda: None
    risk_limits: Callable[[], object | None] = lambda: None
    tracker: Callable[[], object | None] = lambda: None
    accounting: Callable[[], object | None] = lambda: None
    readiness: Callable[[], object | None] = lambda: None
    authority_id: Callable[[], str | None] = lambda: None
    health: Callable[[], Mapping[str, object]] = dict
    risk_rejects: Callable[[], tuple[str, ...]] = tuple
    execution_events: Callable[[], tuple[object, ...]] = tuple
    clock: Callable[[], Milliseconds] = field(default=lambda: 0)
    #: 预测"是否新鲜"由既有 Owner 判定（产品层不重算 TTL）
    prediction_fresh: Callable[[], bool | None] = lambda: None
    #: Run Summary provider（P0001.10.2 §4）：由已记录事实构造；未接线 ⇒ None（不是空报告）
    run_summary: Callable[[], object | None] = lambda: None
    #: F4：按 run_id 读取 **durable** run summary（由既有 RunRegistry + report builder 构建）
    durable_run_summary: Callable[[str], object | None] = lambda run_id: None
    #: Config Provenance（P0001.11 §1）：已解析的 provenance metadata（**不含 secret 值**）
    config_snapshot: Callable[[], object | None] = lambda: None
    #: Run Registry（P0001.11 §2）：产品数据（与 governance 完全分离）
    run_registry: Callable[[], object | None] = lambda: None
    #: 编排层 notes（P0001.11 §4：统一 blocker 投影的输入之一）
    orchestrator_notes: Callable[[], tuple[str, ...]] = tuple
    #: Market Visual Workbench（P0001.12）：有界展示缓冲（只读；不是 market truth）
    market_history: Callable[[], object | None] = lambda: None
    #: 显示参数（window/bucket/max_points/price_levels）；未配置 ⇒ 工作台端点 503（有界是硬要求）
    projection_config: Callable[[], object | None] = lambda: None
    #: local replay session control（只允许作用于 REPLAY runtime）
    replay_control: Callable[[], object | None] = lambda: None
    #: G1：成交事实（只读，由 FillLedger / execution 提供）；`recent_fill_limit=0` 表示不暴露成交
    fills: Callable[[], tuple[object, ...]] = tuple
    recent_fill_limit: int = 0
    #: G2/F5：原始事实查找（kind, identity) -> object | None；**None = 未接线**（区别于"找不到"）
    raw_fact_lookup: Callable[[str, str], object | None] | None = None
    #: G3：账户/敞口时间线缓冲（只读；不是新的 accounting Owner）
    account_timeline: Callable[[], object | None] = lambda: None
    #: G4：prediction provider 状态 / accounting 健康
    prediction_provider_status: Callable[[], object | None] = lambda: None
    accounting_health: Callable[[], object | None] = lambda: None
    #: G5：当前 execution readiness authority（完整事实）
    authority: Callable[[], object | None] = lambda: None
    #: P0001.12.3：Action Plane（Action Gateway）与 Assistant（只读上下文/解释）
    action_gateway: Callable[[], object | None] = lambda: None
    assistant: Callable[[], object | None] = lambda: None
    #: P0001.13：执行安全投影（venue facts / governor / latency / health / reconciliation）
    execution_safety: Callable[[], object | None] = lambda: None
    #: closure Slice 1 / F-11：runtime / loop 状态的只读来源（Owner 是装配入口）
    runtime_status: Callable[[], object | None] = lambda: None
    #: closure Slice 3 / Step 4：稳定 accounting 事实（由 runtime 边界的 provider 提供）
    accounting_facts: Callable[[], object | None] = lambda: None
    #: F-08：真实执行边界的风险判定观测（allow + reject；引用既有 RiskDecision，不复制）
    risk_decisions: Callable[[], tuple[object, ...]] = tuple
    #: F-08：执行边界归一化只读证据（空 ⇒ 如实 ABSENT，不伪造）
    normalization_evidence: Callable[[], tuple[object, ...]] = tuple
    #: F-08：受控 reconciliation 事件（来自真实入口，不是推测）
    reconciliation_events: Callable[[], tuple[object, ...]] = tuple
    #: F-08：ack 延迟来源（Slice 3 已接的真实 observer / BoundedLatencyLog）
    ack_latency: Callable[[], object | None] = lambda: None
    #: F-08：MarketState 的 canonical 指纹（既有 prediction.schema.market_state_hash，经注入避免反向依赖）
    market_state_hash: Callable[[], object | None] = lambda: None
    #: F-12/F-15：operational posture（network/auth/logging/retention + 四层健康拆分）
    ops: Callable[[], Mapping[str, object] | None] = lambda: None

    def run_summary_view(self, run_id: str | None = None) -> object | None:
        """取 Run Summary：`run_id` 给定时走 durable 记录；否则走当前 session provider。

        未接线 ⇒ None（调用方须按 UNKNOWN/503 处理，不得伪造空报告）。
        """
        if run_id:
            return self.durable_run_summary(str(run_id))
        return self.run_summary()

    def market_history_view(self) -> object | None:
        """取有界市场展示缓冲；未接线 ⇒ None（端点须按 UNKNOWN/503 处理）。"""
        return self.market_history()

    def projection_config_view(self) -> object | None:
        """取显示参数；未配置 ⇒ None（工作台端点必须 503，不得无限加载）。"""
        return self.projection_config()

    def replay_control_view(self) -> object | None:
        """取 replay 控制对象（仅 REPLAY runtime 会提供）。"""
        return self.replay_control()

    def execution_safety_view(self) -> object | None:
        """执行安全投影；未接线 ⇒ None（相关端点 503，事实保持 UNKNOWN）。"""
        return self.execution_safety()

    def action_gateway_view(self) -> object | None:
        """Action Plane 入口；未接线 ⇒ None（动作端点 503，绝不静默成功）。"""
        return self.action_gateway()

    def assistant_view(self) -> object | None:
        """Assistant 服务；未接线 ⇒ None。"""
        return self.assistant()

    def account_timeline_view(self) -> object | None:
        """G3：取有界账户序列缓冲；未接线 ⇒ None。"""
        return self.account_timeline()

    def raw_facts_view(self, kind: str, identity: str):
        """G2/F5：取一条原始事实。

        - provider **未接线** ⇒ 抛 `RawFactProviderUnavailable`（API ⇒ 503，不冒充 404）；
        - provider 已接线但找不到 ⇒ `available=False`（API ⇒ 404）；
        - 非法 kind/identity ⇒ `ValueError`（API ⇒ 400）。
        """
        from product.facts import RawFactProviderUnavailable, raw_facts_for

        if not isinstance(kind, str) or not isinstance(identity, str):
            raise ValueError("raw_facts_view requires (kind, identity) strings")
        if self.raw_fact_lookup is None:
            raise RawFactProviderUnavailable(kind)
        return raw_facts_for(kind, identity, self.raw_fact_lookup(kind, identity))

    def run_registry_view(self) -> object | None:
        """取 Run Registry（只读）；未接线 ⇒ None（端点须按 UNKNOWN/503 处理）。"""
        return self.run_registry()

    # ------------------------------------------------------------------ snapshot

    def snapshot(self, *, now_ms: Milliseconds | None = None) -> SystemSnapshot:
        generated_at = self.clock() if now_ms is None else now_ms
        market_state = self.market_state()
        prediction = self.prediction()
        decision = self.maker_decision()
        risk = self.risk_snapshot()
        limits = self.risk_limits()
        tracker = self.tracker()
        accounting = self.accounting()
        readiness = self.readiness()
        health = dict(self.health())
        market = self._market(market_state)   # 单一 Market projection（Monitor / System 共用）

        snapshot = SystemSnapshot(
            generated_at=generated_at,
            runtime=self.identity,
            market=market,
            prediction=self._prediction(prediction),
            strategy=self._strategy(decision),
            risk=self._risk(risk, limits),
            execution=self._execution(tracker, decision),
            portfolio=self._portfolio(accounting),
            readiness=self._readiness(readiness),
            health=self._health(health, market),
            evidence=self._evidence(market_state, prediction, decision, readiness, tracker),
            config=self._config(),
            execution_safety=self._execution_safety(),
            ops=self._ops(),
            blockers=(),
        )
        # Unified Blockers 是**只读投影**：由已组装的事实推导，再替换回快照（不重排、不丢弃）
        return replace(snapshot, blockers=self._blockers(snapshot))

    # ------------------------------------------------------------------ 各段（纯搬运）

    def _market(self, state: object | None) -> MarketView:
        """F2：从既有 `DataQuality` 正式字段映射，**不新增第二套 health 判断**。

        - `book_health`：真实字段（`BookHealth` 枚举，如 healthy/stale/resyncing/awaiting_snapshot）；
        - `healthy`：**只**由 `book_health == healthy` 推导（known 才推导；UNKNOWN 保持 UNKNOWN）；
        - `tradeable`：独立事实，**不用它反推 health**；
        - `freshness`：直接搬运 `book_age_ms`（未知 ⇒ UNKNOWN），不编造阈值。
        """
        quality = _get(state, "quality")
        price = _get(state, "price")
        identity = _get(state, "identity")
        raw_health = _get(quality, "book_health")
        book_health = getattr(raw_health, "value", raw_health)
        healthy = (str(book_health).lower() == "healthy") if book_health is not None else None
        return MarketView(
            healthy=Fact.of(healthy, unknown_reason="book_health is not available"),
            book_health=Fact.of(book_health, unknown_reason="book_health is not available"),
            book_age_ms=Fact.of(_get(quality, "book_age_ms"),
                                unknown_reason="book age is not available"),
            tradeable=Fact.of(_get(quality, "tradeable")),
            window_coverage_ms=Fact.of(_get(quality, "window_coverage_ms")),
            best_bid=Fact.of(_get(price, "best_bid")),
            best_ask=Fact.of(_get(price, "best_ask")),
            spread_bps=Fact.of(_get(price, "spread_bps")),
            market_state_hash=Fact.of(_get(identity, "state_hash")),
        )

    def _prediction(self, record: object | None) -> PredictionView:
        if record is None:
            unknown = Fact.unknown("no prediction record yet")
            return PredictionView(
                request_id=unknown, sequence=unknown, provider=unknown, model=unknown, as_of=unknown,
                expires_at=unknown, latency_ms=unknown, derived_confidence=unknown,
                market_state_hash=unknown, freshest=unknown, horizons=unknown,
            )
        prediction = _get(record, "prediction")
        fresh = self.prediction_fresh()
        raw_horizons = getattr(prediction, "future_return", None)
        horizons = None
        if raw_horizons:
            horizons = tuple(
                {"horizon_ms": int(distribution.horizon_ms),
                 **{category: float(probability)
                    for category, probability in distribution.as_mapping()}}
                for distribution in raw_horizons)
        return PredictionView(
            request_id=Fact.of(_get(record, "request_id")),
            sequence=Fact.of(_get(record, "sequence")),
            provider=Fact.of(_get(record, "provider")),
            model=Fact.of(_get(record, "model")),
            as_of=Fact.of(_get(record, "as_of")),
            expires_at=Fact.of(_get(record, "expires_at")),
            latency_ms=Fact.of(_get(record, "latency_ms")),
            derived_confidence=Fact.of(_get(prediction, "derived_confidence")),
            market_state_hash=Fact.of(_get(record, "market_state_hash")),
            freshest=(Fact.unknown("freshness not evaluated") if fresh is None else Fact.of(fresh)),
            horizons=Fact.of(horizons, unknown_reason="prediction has no horizon distributions"),
        )

    def _strategy(self, decision: object | None) -> StrategyView:
        if decision is None:
            unknown = Fact.unknown("no maker decision yet")
            return StrategyView(at_ms=unknown, mode=unknown, detail=unknown, blocked_by=unknown,
                                bid_action=unknown, ask_action=unknown, bid_price=unknown,
                                bid_quantity=unknown, ask_price=unknown, ask_quantity=unknown)
        bid = _get(decision, "bid")
        ask = _get(decision, "ask")
        reason = _maker_reason(decision)
        return StrategyView(
            at_ms=Fact.of(_get(decision, "at_ms")),
            mode=Fact.of(getattr(decision, "mode", None)),
            detail=Fact.of(_get(decision, "detail")),
            blocked_by=(Fact.unknown("not blocked") if reason is None else Fact.of(reason)),
            bid_action=Fact.of(getattr(_get(bid, "action"), "value", None)),
            ask_action=Fact.of(getattr(_get(ask, "action"), "value", None)),
            bid_price=Fact.of(_get(bid, "price")),
            bid_quantity=Fact.of(_get(bid, "quantity")),
            ask_price=Fact.of(_get(ask, "price")),
            ask_quantity=Fact.of(_get(ask, "quantity")),
        )

    def _risk(self, snapshot: object | None, limits: object | None) -> RiskView:
        kill_switch = _get(snapshot, "kill_switch_mode")
        return RiskView(
            kill_switch_mode=Fact.of(getattr(kill_switch, "value", kill_switch)),
            max_position_qty=Fact.of(_get(limits, "max_position_qty")),
            max_position_notional=Fact.of(_get(limits, "max_position_notional")),
            max_open_order_exposure=Fact.of(_get(limits, "max_open_order_exposure")),
            max_daily_loss=Fact.of(_get(limits, "max_daily_loss")),
            max_drawdown_pct=Fact.of(_get(limits, "max_drawdown_pct")),
            realized_pnl_today=Fact.of(_get(snapshot, "realized_pnl_today")),
            drawdown=Fact.of(_get(snapshot, "drawdown")),
            peak_equity=Fact.of(_get(snapshot, "peak_equity")),
            rejects=tuple(self.risk_rejects()),
        )

    def _execution(self, tracker: object | None, decision: object | None) -> ExecutionView:
        orders = tuple(getattr(tracker, "active", lambda: ())()) if tracker is not None else ()
        decision_id_by_client = self._decision_index(decision)
        views = tuple(
            OrderView(
                client_order_id=str(_get(order, "client_order_id") or ""),
                side=str(getattr(_get(order, "side"), "value", None) or "unknown"),
                status=str(getattr(_get(order, "status"), "value", None) or "unknown"),
                price=Fact.of(_get(order, "price")),
                quantity=Fact.of(_get(order, "quantity")),
                filled_quantity=Fact.of(_get(order, "filled_quantity")),
                reduce_only=bool(_get(order, "reduce_only") or False),
                created_at=int(_get(order, "created_at") or 0),
                updated_at=int(_get(order, "updated_at") or 0),
                decision_id=(Fact.unknown("no matching maker decision")
                             if str(_get(order, "client_order_id") or "") not in decision_id_by_client
                             else Fact.of(decision_id_by_client[str(_get(order, "client_order_id") or "")])),
                uncertain=bool(getattr(order, "status", None) is not None
                               and getattr(getattr(order, "status"), "is_lost", False)),
            )
            for order in orders
        )
        unknown_exposure = (Fact.unknown("tracker not provided") if tracker is None
                            else Fact.of(getattr(tracker, "uncertain_exposure", lambda: None)()))
        pending = (Fact.unknown("tracker not provided") if tracker is None
                   else Fact.of(getattr(tracker, "total_pending_exposure", lambda: None)()))
        fills: list[FillView] = []
        limit = int(self.recent_fill_limit)
        if limit > 0:
            for fill in tuple(self.fills())[-limit:]:
                client_id = _get(fill, "client_order_id")
                fills.append(FillView(
                    client_order_id=Fact.of(client_id, unknown_reason="fill without client order id"),
                    ts=Fact.of(_get(fill, "ts")),
                    price=Fact.of(_get(fill, "price")),
                    quantity=Fact.of(_get(fill, "quantity")),
                    fee=Fact.of(_get(fill, "fee")),
                    trade_id=Fact.of(_get(fill, "trade_id")),
                ))
        return ExecutionView(
            recent_fills=tuple(fills),
            recent_fill_limit=limit,
            active_orders=views,
            uncertain_exposure=unknown_exposure,
            open_order_exposure=pending,
            has_unknown_exposure=bool(getattr(tracker, "has_unknown_exposure", False)),
            unknown_submit_count=Fact.of(_get(tracker, "unknown_submit_count")),
            unknown_cancel_count=Fact.of(_get(tracker, "unknown_cancel_count")),
        )

    def _portfolio(self, accounting: object | None) -> PortfolioView:
        facts = self.accounting_facts()
        if facts is not None:
            # F1：只消费 provider 的 typed facts；不再猜 Fact 内部结构（不写 `is True`）
            return PortfolioView(
                position_qty=getattr(facts, "position_qty",
                                      Fact.unknown("position quantity not provided")),
                average_entry_price=Fact.unknown("not provided by accounting facts"),
                mark_price=Fact.unknown("not provided by accounting facts"),
                unrealized_pnl=getattr(facts, "unrealized_pnl", Fact.unknown("not provided")),
                realized_pnl=getattr(facts, "realized_pnl", Fact.unknown("not provided")),
                fees_paid=Fact.unknown("not provided by accounting facts"),
                funding_paid=Fact.unknown("not provided by accounting facts"),
                balance=getattr(facts, "available_balance", Fact.unknown("not provided")),
                equity=getattr(facts, "equity", Fact.unknown("not provided")),
            )
        position = None
        if accounting is not None:
            getter = getattr(accounting, "position", None)
            if callable(getter):
                try:
                    position = getter(self.identity.symbol)
                except Exception:  # noqa: BLE001 - 读不到就是未知，不是 0
                    position = None
        return PortfolioView(
            position_qty=Fact.of(_get(position, "qty")),
            average_entry_price=Fact.of(_get(position, "average_entry_price")),
            mark_price=Fact.of(_get(position, "mark_price")),
            unrealized_pnl=Fact.of(_get(position, "unrealized_pnl")),
            realized_pnl=Fact.of(_get(position, "realized_pnl")),
            fees_paid=Fact.of(_get(position, "fees_paid")),
            funding_paid=Fact.of(_get(position, "funding_paid")),
            balance=Fact.of(_get(accounting, "balance")),
            equity=Fact.of(_get(accounting, "equity")),
        )

    def _readiness(self, result: object | None) -> ReadinessView:
        if result is None:
            return ReadinessView(status=Fact.unknown("readiness not evaluated"),
                                 scope=Fact.unknown("readiness not evaluated"),
                                 applicable=Fact.unknown("readiness not evaluated"),
                                 authority_id=Fact.of(self.authority_id(),
                                                      unknown_reason="no authority issued"))
        status = _get(result, "status")
        scope = _get(result, "scope")
        authority = self.authority()
        return ReadinessView(
            authority_kind=Fact.of(getattr(getattr(authority, "kind", None), "value",
                                          getattr(authority, "kind", None)),
                                   unknown_reason="no authority issued"),
            authority_issued_at_ms=Fact.of(_get(authority, "issued_at_ms"),
                                           unknown_reason="no authority issued"),
            authority_expires_at_ms=Fact.of(_get(authority, "expires_at_ms"),
                                            unknown_reason="no authority issued"),
            authority_recovery_generation=Fact.of(
                str(_get(authority, "recovery_generation")) if authority is not None else None,
                unknown_reason="no authority issued"),
            authority_market_generation=Fact.of(_get(authority, "market_generation"),
                                                unknown_reason="no authority issued"),
            status=Fact.of(getattr(status, "value", status)),
            scope=Fact.of(getattr(scope, "value", scope)),
            reasons=tuple(r.value if hasattr(r, "value") else str(r) for r in (_get(result, "reasons") or ())),
            details=tuple(str(d) for d in (_get(result, "details") or ())),
            applicable=Fact.of(getattr(result, "applicable", True)),
            authority_id=Fact.of(self.authority_id(), unknown_reason="no authority issued"),
        )

    def _health(self, health: Mapping[str, object], market: MarketView) -> HealthView:
        prediction_status = self.prediction_provider_status()
        facts = self.accounting_facts()
        accounting_status = (facts.health() if facts is not None and hasattr(facts, "health")
                             else self.accounting_health())
        runtime = self.runtime_status()
        unknown = Fact.unknown("runtime state not provided")
        runtime_facts = {
            "runtime_state": Fact.of(getattr(getattr(runtime, "state", None), "value", None)),
            "runtime_detail": Fact.of(getattr(runtime, "detail", None)),
            "runtime_quoting": Fact.of(getattr(runtime, "quoting", None)),
            "runtime_run_id": Fact.of(getattr(runtime, "run_id", None)),
            "runtime_since_ms": Fact.of(getattr(runtime, "since_ms", None)),
        } if runtime is not None else {"runtime_state": unknown, "runtime_detail": unknown,
                                       "runtime_quoting": unknown, "runtime_run_id": unknown,
                                       "runtime_since_ms": unknown}
        return HealthView(
            **runtime_facts,
            prediction_provider=Fact.of(
                getattr(prediction_status, "value", prediction_status)
                if prediction_status is not None else health.get("prediction_provider"),
                unknown_reason="prediction provider status not wired"),
            accounting=Fact.of(accounting_status if not hasattr(accounting_status, "value")
                               else accounting_status.value,
                               unknown_reason="accounting health not wired"),
            # F2：System 与 Monitor 使用**同一** Market projection（book_health 推导），
            # provider 显式给出 market_healthy 时才覆盖。
            market_healthy=(Fact.of(health.get("market_healthy"))
                            if health.get("market_healthy") is not None else market.healthy),
            book_health=market.book_health,
            market_tradeable=market.tradeable,
            private_stream_state=Fact.of(health.get("private_stream_state"),
                                         unknown_reason=UNKNOWN_NOT_AVAILABLE),
            clock_offset_ms=Fact.of(health.get("clock_offset_ms")),
            uptime_ms=Fact.of(health.get("uptime_ms")),
            notes=tuple(str(n) for n in (health.get("notes") or ())),
        )

    def _ops(self) -> OpsView:
        """F-12/F-15：ops 姿态（未接线 ⇒ 全 UNKNOWN；四层健康彼此独立）。"""
        payload = self.ops()
        if not isinstance(payload, Mapping):
            return OpsView()
        reasons = payload.get("trade_readiness_reasons")
        return OpsView(
            process_live=Fact.of(payload.get("process_live")),
            runtime_state=Fact.of(payload.get("runtime_state")),
            runtime_detail=Fact.of(payload.get("runtime_detail")),
            trade_readiness=Fact.of(payload.get("trade_readiness")),
            trade_readiness_reasons=tuple(str(item) for item in (reasons or ())),
            execution_health=Fact.of(payload.get("execution_health")),
            operational_warning=Fact.of(payload.get("operational_warning")),
            network=Fact.of(payload.get("network")),
            logging=Fact.of(payload.get("logging")),
            retention=Fact.of(payload.get("retention")),
            ts=Fact.of(payload.get("ts")),
        )

    def _config(self) -> ConfigView:
        """Config Provenance 视图（只有非敏感值 + secret 引用名）。"""
        snapshot = self.config_snapshot()
        if snapshot is None:
            unknown = Fact.unknown("no config snapshot recorded")
            return ConfigView(config_id=unknown, fingerprint=unknown, created_at=unknown)
        entries = tuple(
            ConfigEntryView(name=entry.name, source=entry.source.value, value=entry.value,
                            secret_ref=entry.secret_ref)
            for entry in getattr(snapshot, "entries", ())
        )
        return ConfigView(
            config_id=Fact.of(getattr(snapshot, "config_id", None)),
            fingerprint=Fact.of(getattr(snapshot, "fingerprint", None)),
            created_at=Fact.of(getattr(snapshot, "created_at", None)),
            sources=dict(getattr(snapshot, "sources", lambda: {})()) if callable(
                getattr(snapshot, "sources", None)) else dict(getattr(snapshot, "sources", {}) or {}),
            secret_refs=tuple(getattr(snapshot, "secret_refs", lambda: ())()) if callable(
                getattr(snapshot, "secret_refs", None)) else tuple(getattr(snapshot, "secret_refs", ()) or ()),
            entries=entries,
        )

    def _blockers(self, snapshot: SystemSnapshot) -> tuple[BlockerView, ...]:
        """统一 blocker 投影（readiness / risk / strategy / orchestrator / market / prediction / execution）。"""
        base = project_blockers(snapshot=snapshot, orchestrator_notes=tuple(self.orchestrator_notes()))
        projection = self.execution_safety()
        if projection is None:
            return base
        from product.blockers import dedupe_blockers

        return dedupe_blockers((*base, *projection.blockers()))

    def _execution_safety(self) -> ExecutionSafetyView:
        """把执行安全投影映射成产品视图（只搬运；未接线 ⇒ 全 UNKNOWN）。"""
        projection = self.execution_safety()
        if projection is None:
            return ExecutionSafetyView()
        from product.serialization import to_jsonable

        health = projection.health()
        governor = projection.rate_limits()
        latency = projection.latency()
        return ExecutionSafetyView(
            health_status=Fact.of(health.status.value),
            health_reasons=tuple(health.reasons),
            request_budget=(Fact.unknown("execution safety policy not configured") if governor is None
                            else Fact.of(to_jsonable(governor.request))),
            order_budget=(Fact.unknown("execution safety policy not configured") if governor is None
                          else Fact.of(to_jsonable(governor.order))),
            venue_limits=Fact.of(to_jsonable(projection.limits())),
            latency=(Fact.unknown("execution safety policy not configured") if latency is None
                     else Fact.of(to_jsonable(latency))),
            reconciliation=Fact.of(to_jsonable(projection.reconciliation())),
            anomalies=Fact.of(to_jsonable(projection.blockers())),
        )

    # ------------------------------------------------------------------ evidence

    def _decision_index(self, decision: object | None) -> dict[str, str]:
        """client_order_id → decision identity（订单↔决策回溯，SC-7）。"""
        index: dict[str, str] = {}
        if decision is None:
            return index
        at_ms = _get(decision, "at_ms")
        for side_name in ("bid", "ask"):
            side = _get(decision, side_name)
            client_id = _get(side, "client_order_id")
            if isinstance(client_id, str) and client_id:
                index[client_id] = f"maker:{at_ms}:{side_name}"
        return index

    def _evidence(self, state: object | None, prediction: object | None, decision: object | None,
                  readiness: object | None, tracker: object | None) -> EvidenceView:
        """F-08：把既有 owner 事实串成**一条按时间排序的因果链**。

        规则：
        - 只搬运既有事实（不建第二套 evidence store、不复制 Risk/Execution/Reconciliation）；
        - stage 缺事实时**显式**给出 outcome/ABSENT + reason（不静默跳过）；
        - 每个 entry 带 canonical `identity_kind`（client_order_id / fill_id / ...）。
        """
        active = tuple(getattr(tracker, "active", lambda: ())()) if tracker is not None else ()
        decision_index = self._decision_index(decision)
        entries: list[TraceEntry] = []

        def add(stage: str, ts: object, *, identity: object, identity_kind: str, outcome: str,
                reason_code: Fact, detail: str = "", latency: Fact | None = None,
                unknown_reason: str = "fact not available at this stage") -> None:
            entries.append(TraceEntry(
                stage=stage, ts=Fact.of(ts, unknown_reason=unknown_reason),
                identity=Fact.of(identity, unknown_reason=unknown_reason),
                identity_kind=identity_kind, outcome=outcome, reason_code=reason_code, detail=detail,
                latency_ms=(latency if latency is not None
                            else Fact.unknown("no latency fact at this stage")),
            ))

        # market → prediction → decision（既有三阶段，语义不变）
        state_time = _get(state, "time")
        add("market_state", _get(state_time, "as_of_exchange_ts"),
            identity=(self.market_state_hash() if state is not None else None),
            identity_kind="market_state_hash",
            outcome=("unknown" if state is None else str(bool(_get(_get(state, "quality"), "tradeable")))),
            reason_code=Fact.unknown("market gate is a boolean fact, not a reason code"),
            unknown_reason=("market state has no canonical hash yet" if state is not None
                            else "no market state yet"))
        add("prediction", _get(prediction, "as_of"), identity=_get(prediction, "request_id"),
            identity_kind="prediction_request_id",
            outcome=("absent" if prediction is None else "present"),
            reason_code=Fact.unknown("no prediction record yet (downstream stages show why)"
                                     if prediction is None else "no reason code at this stage"),
            detail=str(_get(prediction, "provider") or ""),
            unknown_reason="no prediction record yet" if prediction is None else "no timestamp")
        blocked_code = _maker_reason(decision)
        add("maker_decision", _get(decision, "at_ms"), identity=_get(prediction, "request_id"),
            identity_kind="prediction_request_id",
            outcome=(str(getattr(_get(decision, "mode"), "value", "unknown")) if decision is not None
                     else "absent"),
            reason_code=Fact.of(blocked_code,
                                unknown_reason=("no maker decision yet" if decision is None
                                                else "not blocked")),
            detail=str(_get(decision, "detail") or ""),
            unknown_reason="no maker decision yet" if decision is None else "no timestamp")

        # risk（F-08 新增）：allow 与 reject 都记录（引用既有 RiskDecision）
        for observation in tuple(self.risk_decisions()):
            rd = _get(observation, "decision")
            code = _get(rd, "reason_code")
            client_id = _get(observation, "client_order_id")
            add("risk", _get(observation, "timestamp"), identity=client_id,
                identity_kind="client_order_id",
                outcome=("allow" if code is None else "reject"),
                reason_code=(Fact.unknown("risk decision allowed")
                             if code is None else Fact.of(getattr(code, "value", code))),
                detail=("" if code is None else str(getattr(code, "value", code))),
                unknown_reason="proposal has no order identity (rejected before order creation)")

        # readiness
        reasons = (() if readiness is None
                   else tuple(r.value if hasattr(r, "value") else str(r)
                              for r in (_get(readiness, "reasons") or ())))
        applicable = bool(getattr(readiness, "applicable", True))
        add("readiness", _get(readiness, "evaluated_at_ms") or _get(readiness, "at_ms"),
            identity=(self.authority_id() or str(getattr(_get(readiness, "status"), "value", "unknown"))),
            identity_kind="authority_id",
            outcome=("absent" if readiness is None else
                     ("not_applicable" if not applicable else ("blocked" if reasons else "ready"))),
            reason_code=Fact.of(reasons[0]) if reasons else Fact.unknown("no blocker"),
            detail="; ".join(reasons[:3]),
            unknown_reason="readiness not evaluated" if readiness is None else "readiness has no timestamp")

        # normalization（F-08 新增；空 ⇒ 如实 ABSENT）
        for evidence in tuple(self.normalization_evidence()):
            rejected = bool(_get(evidence, "rejected"))
            rounding = f"tick={_get(evidence, 'price_rounding')} step={_get(evidence, 'quantity_rounding')}"
            if rejected:
                outcome, code, detail = "rejected", Fact.of(str(_get(evidence, "reject_reason"))), rounding
            else:
                outcome = "adjusted" if _get(evidence, "adjusted") else "normalized"
                code = Fact.unknown("normalization accepted")
                detail = (f"{_get(evidence, 'input_price')} -> {_get(evidence, 'normalized_price')} | "
                          f"{_get(evidence, 'input_quantity')} -> {_get(evidence, 'normalized_quantity')} | {rounding}")
            add("normalization", _get(evidence, "ts"), identity=_get(evidence, "client_order_id"),
                identity_kind="client_order_id", outcome=outcome, reason_code=code, detail=detail,
                unknown_reason="normalization evidence has no client order id")

        # order（submit 阶段）：包含 active 与已终态订单（终态订单才是真实提交过的事实）
        candidate = getattr(tracker, "orders", None) if tracker is not None else None
        if callable(candidate):
            all_orders = tuple(candidate())
        elif candidate is not None:
            all_orders = tuple(candidate)
        else:
            all_orders = active
        for order in all_orders:
            client_id = str(_get(order, "client_order_id") or "")
            decision_ref = decision_index.get(client_id)
            add("order", _get(order, "created_at"), identity=client_id,
                identity_kind="client_order_id",
                outcome=str(getattr(_get(order, "status"), "value", "unknown")),
                reason_code=Fact.unknown("order carries no rejection reason code"),
                detail=(f"decision_id={decision_ref}" if decision_ref
                        else "no matching maker decision"),
                unknown_reason="order without client_order_id")

        # ack（F-08 新增）：只读 Slice 3 已接的真实 observer
        observer = self.ack_latency()
        latency_log = getattr(observer, "log", observer)
        samples = getattr(latency_log, "samples", None)
        if callable(samples):
            for stage_name, label in (("submit_to_ack", "acked"), ("cancel_to_ack", "cancel_acked")):
                for sample in samples(stage=stage_name):
                    sample_ts = int(getattr(sample, "ts", 0))
                    candidates = [(int(_get(o, "created_at") or 0), str(_get(o, "client_order_id") or ""))
                                  for o in all_orders if int(_get(o, "created_at") or 0) <= sample_ts]
                    client_id = max(candidates)[1] if candidates else None
                    add("ack", sample_ts, identity=client_id, identity_kind="client_order_id",
                        outcome=label, reason_code=Fact.unknown("ack carries no rejection reason code"),
                        detail=f"{stage_name}={getattr(sample, 'value_ms', None)} ms",
                        latency=Fact.of(getattr(sample, "value_ms", None)),
                        unknown_reason="ack could not be paired to an active order")

        # execution_event / cancel
        for event in self.execution_events():
            client_id = str(getattr(event, "client_order_id", "") or "") or None
            event_name = str(_get(event, "event_name") or type(event).__name__)
            reason = str(getattr(event, "reason", "") or "")
            add("execution_event", getattr(event, "timestamp", None), identity=client_id,
                identity_kind="client_order_id", outcome=event_name,
                reason_code=(Fact.of(reason) if reason else Fact.unknown("no reason attached")),
                unknown_reason="execution event without client_order_id")
            if event_name in ("OrderCanceled", "OrderStatus:CANCELED"):
                add("cancel", getattr(event, "timestamp", None), identity=client_id,
                    identity_kind="client_order_id", outcome="canceled",
                    reason_code=Fact.unknown("cancel carries no rejection reason code"),
                    detail="confirmed by execution event (request != success)")

        # fill
        limit = int(self.recent_fill_limit)
        if limit > 0:
            for fill in tuple(self.fills())[-limit:]:
                add("fill", _get(fill, "ts"), identity=_get(fill, "client_order_id"),
                    identity_kind="client_order_id", outcome="filled",
                    reason_code=Fact.unknown("fill carries no rejection reason code"),
                    detail=f"fill_id={_get(fill, 'trade_id')}",
                    unknown_reason="fill without client order id")

        # unknown（聚合计数；不伪装成某笔订单）
        unknown_submit = int(_get(tracker, "unknown_submit_count") or 0)
        unknown_cancel = int(_get(tracker, "unknown_cancel_count") or 0)
        if unknown_submit or unknown_cancel:
            add("unknown", None, identity=None, identity_kind="aggregate_count",
                outcome=f"submit={unknown_submit} cancel={unknown_cancel}",
                reason_code=Fact.unknown("unknown outcomes are counted, not attributed"),
                detail="UNKNOWN != rejected/canceled; converge via query/reconciliation",
                unknown_reason="aggregate count has no single timestamp/identity")

        # reconciliation（F-08 新增；来自受控入口的真实事件）
        for event in tuple(self.reconciliation_events()):
            add("reconciliation", _get(event, "ts"), identity=_get(event, "identity"),
                identity_kind=str(_get(event, "identity_kind") or "reconciliation"),
                outcome=str(_get(event, "outcome") or "requested"),
                reason_code=Fact.of(str(_get(event, "reason_code") or ""),
                                    unknown_reason="no reason code attached"),
                detail=str(_get(event, "detail") or ""),
                unknown_reason="reconciliation event without timestamp")

        # F-08：没有事实的 canonical 阶段必须**显式**出现（ABSENT + reason），不得静默跳过。
        # `fill` 例外：`recent_fill_limit == 0` 表示"未暴露成交"（接线状态），不是"没有成交"。
        explicit_absent = ("risk", "normalization", "order", "ack", "execution_event", "cancel",
                           "unknown", "reconciliation")
        present = {entry.stage for entry in entries}
        for stage in explicit_absent:
            if stage in present:
                continue
            entries.append(TraceEntry(
                stage=stage, ts=Fact.unknown(f"no {stage} fact recorded yet"),
                identity=Fact.unknown(f"no {stage} identity recorded yet"), identity_kind="",
                outcome="absent",
                reason_code=Fact.unknown(f"no {stage} fact recorded yet (explicit ABSENT, not skipped)"),
                detail="", latency_ms=Fact.unknown("no latency fact at this stage")))

        return EvidenceView(
            trace=self._order_trace(entries),
            readiness_blockers=reasons,
            risk_rejects=tuple(self.risk_rejects()),
            notes=("product layer only composes existing owner facts (single evidence view)",),
        )

    @staticmethod
    def _order_trace(entries: list[TraceEntry]) -> tuple[TraceEntry, ...]:
        """按时间排序；带时间的事实按 ts，缺时间的按因果阶段顺序（carry-forward）。"""
        stage_order = ("market_state", "prediction", "maker_decision", "risk", "readiness",
                       "normalization", "order", "ack", "execution_event", "cancel", "fill",
                       "unknown", "reconciliation")
        carried: int | None = None
        keyed: list[tuple[tuple[int, int, int], TraceEntry]] = []
        for index, entry in enumerate(entries):
            if entry.ts.known:
                carried = int(entry.ts.value)  # type: ignore[arg-type]
            resolved = carried if carried is not None else -1
            rank = stage_order.index(entry.stage) if entry.stage in stage_order else len(stage_order)
            keyed.append(((resolved, rank, index), entry))
        keyed.sort(key=lambda item: item[0])
        return tuple(entry for _, entry in keyed)
