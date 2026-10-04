"""MakerPolicy：把市场状态 + 预测 + 持仓 + 风险快照转成报价决策（P0001.7）。

职责边界：

- **只产出决策**：`MakerDecision`（含 `OrderProposal`，永远 post-only）。不做撤单、不做下单、不看网络。
- 不修改 Accounting / Execution 状态机；不发明任何经济参数（全部来自 `MakerPolicyConfig`）。
- RiskGate 仍是最终权威：策略层的规模上限、库存偏置、cost floor 都不替代 Risk 判定。
- 时间只来自 `RiskSnapshot.now_ms`（不使用 wall-clock）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from execution.types import Order
from market.state.types import MarketState
from portfolio.position import Position
from portfolio.types import Side
from prediction.types import PredictionRecord
from market.events.types import Milliseconds
from risk.types import KillSwitchMode, OrderCorrelation, RiskSnapshot
from strategy.maker.inventory import InventoryBias, compute_inventory_bias
from strategy.maker.lifecycle import SidePlan, plan_side_action
from strategy.maker.pricing import PricePlan, plan_quote_price
from strategy.maker.sizing import SizePlan, plan_quote_size
from strategy.maker.types import MakerDecision, MakerPolicyConfig, QuoteDecision, QuoteTrigger

_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class GlobalGate:
    """全局门结果（fail closed）。"""

    trigger: QuoteTrigger | None
    detail: str
    allow_new_increasing: bool
    allow_new_reducing: bool
    cancel_increasing_quotes: bool
    cancel_all_quotes: bool

    @property
    def normal(self) -> bool:
        return self.trigger is None


def _normal_gate() -> GlobalGate:
    return GlobalGate(
        trigger=None,
        detail="",
        allow_new_increasing=True,
        allow_new_reducing=True,
        cancel_increasing_quotes=False,
        cancel_all_quotes=False,
    )


def _blocked_gate(
    trigger: QuoteTrigger, detail: str, *, allow_new_reducing: bool = False, cancel_all: bool = False
) -> GlobalGate:
    """全局门默认**不允许任何新报价**（连 reduce-only 也不新增），只保留已有的 reduce-only 挂单。

    例外：prediction 不可用（`PREDICTION_STALE`）与 `REDUCE_ONLY` 熔断只禁止**新增暴露**，
    允许新挂 reduce-only 报价 —— 降低 exposure 不得被预测可用性阻断（P0001.7.1）。
    """
    return GlobalGate(
        trigger=trigger,
        detail=detail,
        allow_new_increasing=False,
        allow_new_reducing=allow_new_reducing,
        cancel_increasing_quotes=not cancel_all,
        cancel_all_quotes=cancel_all,
    )


def fresh_prediction(
    *,
    prediction: PredictionRecord | None,
    config: MakerPolicyConfig,
    now_ms: Milliseconds,
) -> PredictionRecord | None:
    """可用于**方向性调整**的 prediction；不可用（缺失 / 过期 / 缺 horizon）时返回 None。

    P0001.7.1：prediction 不可用只影响「新增暴露」与方向性调整，绝不阻断 reduce-only 报价；
    同时**不得伪造 prediction**（例如沿用过期分布做方向判断）。
    """
    usable, _ = prediction_verdict(prediction=prediction, horizon_ms=config.prediction_horizon_ms, now_ms=now_ms)
    return prediction if usable else None


def prediction_verdict(
    *, prediction: PredictionRecord | None, horizon_ms: int, now_ms: int
) -> tuple[bool, str]:
    """预测是否可用于**新增暴露**（freshness + 必需 horizon）。"""
    if prediction is None:
        return False, "no prediction record available"
    if prediction.is_expired(now_ms):
        return False, f"prediction expired at {prediction.expires_at} (now {now_ms})"
    if prediction.prediction.distribution(horizon_ms) is None:
        return False, f"prediction has no horizon {horizon_ms}ms"
    return True, ""


def evaluate_global_gate(
    *,
    state: MarketState,
    prediction: PredictionRecord | None,
    snapshot: RiskSnapshot,
    kill_switch: KillSwitchMode,
    config: MakerPolicyConfig,
) -> GlobalGate:
    """按固定优先级评估全局门（首个命中者生效）。"""
    if kill_switch is KillSwitchMode.HALT_ALL:
        return _blocked_gate(QuoteTrigger.KILL_SWITCH, "kill switch is HALT_ALL: no new quotes", cancel_all=True)
    if not state.quality.tradeable:
        return _blocked_gate(
            QuoteTrigger.MARKET_UNHEALTHY,
            f"market data is not tradeable (book_health={state.quality.book_health.value})",
        )
    fresh, detail = prediction_verdict(
        prediction=prediction, horizon_ms=config.prediction_horizon_ms, now_ms=snapshot.now_ms
    )
    if not fresh:
        # P0001.7.1：prediction 只是「新增暴露」的必要条件，不是「降低暴露」的必要条件。
        # 否则 Jev 故障 + 无存量退出挂单时，已有仓位将无法主动降险。
        return _blocked_gate(QuoteTrigger.PREDICTION_STALE, detail, allow_new_reducing=True)
    if snapshot.unresolved_order_count > 0:
        return _blocked_gate(
            QuoteTrigger.UNKNOWN_EXPOSURE,
            f"{snapshot.unresolved_order_count} order(s) have unknown exposure (P0001.6.1)",
        )
    if kill_switch is KillSwitchMode.REDUCE_ONLY:
        # REDUCE_ONLY 的语义就是「允许降风险」：允许新挂 reduce-only 报价
        return _blocked_gate(
            QuoteTrigger.KILL_SWITCH,
            "kill switch is REDUCE_ONLY: only risk-reducing quotes",
            allow_new_reducing=True,
        )
    return _normal_gate()


class MakerPolicy:
    """确定性的 Maker 报价策略（单品种、市场中性库存控制）。"""

    def __init__(self, config: MakerPolicyConfig) -> None:
        if not isinstance(config, MakerPolicyConfig):
            raise ValueError(f"config must be a MakerPolicyConfig, got {type(config).__name__}")
        self._config = config

    @property
    def config(self) -> MakerPolicyConfig:
        return self._config

    def decide(
        self,
        *,
        state: MarketState,
        prediction: PredictionRecord | None,
        position: Position,
        snapshot: RiskSnapshot,
        existing_orders: tuple[Order, ...],
        remaining_risk_budget: float | None,
        kill_switch: KillSwitchMode,
    ) -> MakerDecision:
        """产出一轮双边 Maker 决策。相同输入必定得到相同输出。"""
        _validate_inputs(
            state=state,
            prediction=prediction,
            position=position,
            snapshot=snapshot,
            existing_orders=existing_orders,
            remaining_risk_budget=remaining_risk_budget,
            kill_switch=kill_switch,
        )
        decision_id = self._decision_identity(state=state, prediction=prediction, now_ms=snapshot.now_ms)
        correlation = self._correlation(decision_id=decision_id, state=state, prediction=prediction)
        gate = evaluate_global_gate(
            state=state,
            prediction=prediction,
            snapshot=snapshot,
            kill_switch=kill_switch,
            config=self._config,
        )
        plans = self._side_plans(
            state=state,
            prediction=prediction,
            gate=gate,
            now_ms=snapshot.now_ms,
            position_qty=position.qty,
            remaining_risk_budget=remaining_risk_budget,
        )
        decisions = self._side_decisions(
            plans=plans,
            state=state,
            gate=gate,
            now_ms=snapshot.now_ms,
            existing_orders=existing_orders,
            correlation=correlation,
        )
        return _assemble(
            symbol=state.identity.symbol,
            now_ms=snapshot.now_ms,
            plans=plans,
            decisions=decisions,
            gate=gate,
            decision_id=decision_id,
        )

    # ------------------------------------------------------------------ correlation（P0001.15 §15）

    def _decision_identity(self, *, state: MarketState, prediction: PredictionRecord | None,
                           now_ms: Milliseconds) -> str:
        """稳定 / 确定性的决策身份：同一输入 ⇒ 同一 id（无需额外 owner 维护计数器）。

        直接复用既有 `market_state_hash`（内容寻址摘要）的摘要段 + 决策时刻，
        因此本层无需引入新的哈希依赖，也不需要 runtime 侧计数器。
        """
        from prediction.schema import market_state_hash

        digest = market_state_hash(state).split(":", 1)[-1][:12]
        return f"d-{digest}-{int(now_ms)}"

    def _correlation(self, *, decision_id: str, state: MarketState,
                     prediction: PredictionRecord | None) -> OrderCorrelation | None:
        """correlation metadata：instrument / venue 由 config 注入；缺身份 ⇒ 不伪造（返回 None）。"""
        if not self._config.instrument_id and not self._config.venue_id:
            return None
        from prediction.schema import market_state_hash

        return OrderCorrelation(
            decision_id=decision_id,
            instrument_id=self._config.instrument_id or None,
            venue_id=self._config.venue_id or None,
            prediction_id=None if prediction is None else str(prediction.request_id),
            market_state_hash=market_state_hash(state))

    def _side_plans(
        self,
        *,
        state: MarketState,
        prediction: PredictionRecord | None,
        gate: GlobalGate,
        now_ms: Milliseconds,
        position_qty: float,
        remaining_risk_budget: float | None,
    ) -> tuple[SidePlan, SidePlan]:
        bias = compute_inventory_bias(position_qty=position_qty, config=self._config)
        usable = fresh_prediction(prediction=prediction, config=self._config, now_ms=now_ms)
        plans = tuple(
            self._side_plan(
                side=side,
                state=state,
                prediction=usable,
                gate=gate,
                bias=bias,
                position_qty=position_qty,
                remaining_risk_budget=remaining_risk_budget,
            )
            for side in (Side.BUY, Side.SELL)
        )
        return (plans[0], plans[1])

    def _side_decisions(
        self,
        *,
        plans: tuple[SidePlan, SidePlan],
        state: MarketState,
        gate: GlobalGate,
        now_ms: int,
        existing_orders: tuple[Order, ...],
        correlation: OrderCorrelation | None = None,
    ) -> tuple[QuoteDecision, QuoteDecision]:
        decisions = tuple(
            plan_side_action(
                plan=plan,
                existing=_existing_for(side=plan.side, orders=existing_orders),
                symbol=state.identity.symbol,
                config=self._config,
                now_ms=now_ms,
                gate_trigger=gate.trigger,
                cancel_increasing=gate.cancel_increasing_quotes,
                cancel_all=gate.cancel_all_quotes,
                correlation=correlation,
            )
            for plan in plans
        )
        return (decisions[0], decisions[1])

    def _quote_prices(
        self,
        *,
        side: Side,
        state: MarketState,
        prediction: PredictionRecord | None,
        reduce_only: bool,
        extra_retreat_ticks: int,
    ) -> PricePlan:
        return plan_quote_price(
            side=side,
            state=state,
            prediction=None if prediction is None else prediction.prediction,
            config=self._config,
            reduce_only=reduce_only,
            extra_retreat_ticks=extra_retreat_ticks,
        )

    def _quote_sizes(
        self,
        *,
        price_plan: PricePlan,
        side: Side,
        inventory_factor: float,
        prediction: PredictionRecord | None,
        remaining_risk_budget: float | None,
        reduce_only: bool,
        position_qty: float,
    ) -> SizePlan:
        return plan_quote_size(
            side=side,
            price=price_plan.price if price_plan.price is not None else 0.0,
            inventory_factor=inventory_factor,
            derived_confidence=0.0 if prediction is None else prediction.prediction.derived_confidence,
            remaining_risk_budget=remaining_risk_budget,
            reduce_only=reduce_only,
            position_qty=position_qty,
            config=self._config,
        )

    def _blocked_side(
        self, side: Side, *, reduce_only: bool, trigger: QuoteTrigger | None, detail: str, reasons: tuple[str, ...] = ()
    ) -> SidePlan:
        return SidePlan(
            side=side,
            desired_price=None,
            desired_quantity=None,
            reduce_only=reduce_only,
            forbidden=trigger or QuoteTrigger.SIDE_NOT_PERMITTED,
            forbidden_detail=detail,
            reasons=reasons,
        )

    # ------------------------------------------------------------------ 内部

    def _side_plan(
        self,
        *,
        side: Side,
        state: MarketState,
        prediction: PredictionRecord | None,
        gate: GlobalGate,
        bias: InventoryBias,
        position_qty: float,
        remaining_risk_budget: float | None,
    ) -> SidePlan:
        reduce_only = is_reduce_only(side=side, position_qty=position_qty, config=self._config)
        allowed = gate.allow_new_reducing if reduce_only else gate.allow_new_increasing
        if not allowed:
            return self._blocked_side(
                side,
                reduce_only=reduce_only,
                trigger=gate.trigger,
                detail=gate.detail or "new quotes are not allowed on this side",
            )

        return self._price_and_size(
            side=side,
            state=state,
            prediction=prediction,
            bias=bias,
            reduce_only=reduce_only,
            position_qty=position_qty,
            remaining_risk_budget=remaining_risk_budget,
        )

    def _price_and_size(
        self,
        *,
        side: Side,
        state: MarketState,
        prediction: PredictionRecord | None,
        bias: InventoryBias,
        reduce_only: bool,
        position_qty: float,
        remaining_risk_budget: float | None,
    ) -> SidePlan:
        buy_side = side is Side.BUY
        price_plan = self._quote_prices(
            side=side,
            state=state,
            prediction=prediction,
            reduce_only=reduce_only,
            extra_retreat_ticks=bias.buy_extra_retreat_ticks if buy_side else bias.sell_extra_retreat_ticks,
        )
        if not price_plan.permitted or price_plan.price is None:
            return _blocked_from_plan(side, reduce_only=reduce_only, plan=price_plan)
        size_plan = self._quote_sizes(
            price_plan=price_plan,
            side=side,
            inventory_factor=bias.buy_size_factor if buy_side else bias.sell_size_factor,
            prediction=prediction,
            remaining_risk_budget=remaining_risk_budget,
            reduce_only=reduce_only,
            position_qty=position_qty,
        )
        if not size_plan.permitted or size_plan.quantity is None:
            return _blocked_from_plan(
                side, reduce_only=reduce_only, plan=size_plan, extra_reasons=price_plan.reasons
            )
        return SidePlan(
            side=side,
            desired_price=price_plan.price,
            desired_quantity=size_plan.quantity,
            reduce_only=reduce_only,
            forbidden=None,
            forbidden_detail="",
            reasons=price_plan.reasons + size_plan.reasons,
        )


def _blocked_from_plan(
    side: Side,
    *,
    reduce_only: bool,
    plan: PricePlan | SizePlan,
    extra_reasons: tuple[str, ...] = (),
) -> SidePlan:
    """价格或规模不可用 → 该侧本轮不报价（fail closed）。"""
    return SidePlan(
        side=side,
        desired_price=None,
        desired_quantity=None,
        reduce_only=reduce_only,
        forbidden=plan.trigger or QuoteTrigger.SIDE_NOT_PERMITTED,
        forbidden_detail="; ".join(plan.reasons),
        reasons=extra_reasons + plan.reasons,
    )


def _assemble(
    *,
    symbol: str,
    now_ms: int,
    plans: tuple[SidePlan, SidePlan],
    decisions: tuple[QuoteDecision, QuoteDecision],
    gate: GlobalGate,
    decision_id: str = "",
) -> MakerDecision:
    return MakerDecision(
        symbol=symbol,
        at_ms=now_ms,
        bid=decisions[0],
        ask=decisions[1],
        bid_desired=plans[0].desired,
        ask_desired=plans[1].desired,
        blocked_by=gate.trigger,
        detail=gate.detail,
        decision_id=decision_id,
    )


def is_reduce_only(*, side: Side, position_qty: float, config: MakerPolicyConfig) -> bool:
    """该侧是否只可能把持仓推向 target（§0.7）。"""
    if side is Side.SELL:
        return position_qty > config.target_position
    return position_qty < config.target_position


def _existing_for(*, side: Side, orders: tuple[Order, ...]) -> Order | None:
    """该侧最新的非终态挂单（按 created_at + client_order_id 确定性排序）。"""
    candidates = [order for order in orders if order.side is side and not order.is_terminal]
    if not candidates:
        return None
    return max(candidates, key=lambda order: (order.created_at, order.client_order_id))


def _validate_inputs(
    *,
    state: MarketState,
    prediction: PredictionRecord | None,
    position: Position,
    snapshot: RiskSnapshot,
    existing_orders: tuple[Order, ...],
    remaining_risk_budget: float | None,
    kill_switch: KillSwitchMode,
) -> None:
    symbol = state.identity.symbol
    if snapshot.symbol != symbol:
        raise ValueError(f"snapshot.symbol {snapshot.symbol!r} does not match market state symbol {symbol!r}")
    if position.symbol != symbol:
        raise ValueError(f"position.symbol {position.symbol!r} does not match market state symbol {symbol!r}")
    if abs(position.qty - snapshot.position_qty) > _EPSILON:
        raise ValueError(
            f"position.qty {position.qty} is inconsistent with snapshot.position_qty {snapshot.position_qty}"
        )
    if not isinstance(kill_switch, KillSwitchMode):
        raise ValueError(f"kill_switch must be a KillSwitchMode, got {type(kill_switch).__name__}")
    if remaining_risk_budget is not None:
        if isinstance(remaining_risk_budget, bool) or not isinstance(remaining_risk_budget, (int, float)):
            raise ValueError("remaining_risk_budget must be a number or None")
        budget = float(remaining_risk_budget)
        if not math.isfinite(budget) or budget < 0.0:
            raise ValueError(f"remaining_risk_budget must be a non-negative finite number, got {budget!r}")
    for order in existing_orders:
        if not isinstance(order, Order):
            raise ValueError(f"existing_orders must contain Order instances, got {type(order).__name__}")
        if order.symbol != symbol:
            raise ValueError(f"order {order.client_order_id!r} belongs to {order.symbol!r}, not {symbol!r}")
    if prediction is not None and not isinstance(prediction, PredictionRecord):
        raise ValueError(f"prediction must be a PredictionRecord or None, got {type(prediction).__name__}")


__all__ = [
    "GlobalGate",
    "MakerPolicy",
    "evaluate_global_gate",
    "fresh_prediction",
    "is_reduce_only",
    "prediction_verdict",
]
