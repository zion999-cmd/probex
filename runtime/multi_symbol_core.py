"""P0017.5：多标的模拟交易的**共享步进核心**（离线回测与实时运行唯一实现）。

本模块不做 IO、不建线程、不读 wall-clock、不碰真实交易所。它只组合既有 Owner：

- per-symbol `FeatureEngine`（市场可见状态隔离）；
- per-symbol `SimulatedVenue`（经 `MultiSymbolSimulatedAdapter` 路由，执行状态隔离）；
- 一个共享 `ExecutionEngine`（`RiskGate` 含 P0017.2 组合 gross 硬闸 + 一个 `AccountingCore`）。

离线 driver（`backtest/multi_driver.py`）与实时 runner（`runtime/live_multi_symbol.py`）
都调用这里的 `process_market_event`，因此两条路径的决策/风险/执行/会计步进**不存在两份实现**
（P0017.5 §4-B1）。

顺序契约（与 P0017.4 一致）：对每个市场事件
  1. `engine.poll(now)` 结算在途成交（先于本事件决策）；
  2. mark → 更新 accounting mark；book/trade → FeatureEngine + 对应 SimulatedVenue；
  3. 该 symbol 可交易且事件类型命中 decide_on ⇒ MakerPolicy 决策 ⇒ engine.submit；
任何提交都在引擎的同一同步临界区先过组合闸、再过单标的闸。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from execution.engine import ExecutionEngine
from execution.tracker import OrderTracker
from market.events.types import EventType, MarketEvent
from market.features.engine import FeatureEngine
from market.state.types import MarketState
from portfolio.accounting import AccountingCore
from prediction.invoke import invoke_provider_record
from risk.budget import remaining_exposure_budget
from risk.portfolio_budget import PortfolioCommitment
from strategy.maker.types import QuoteAction


@dataclass
class StepCounters:
    """跨路径中立的步进计数（离线/实时共用；不依赖回测状态枚举）。"""

    decisions: int = 0
    submits: int = 0
    risk_rejects: int = 0
    fills: int = 0
    events: int = 0
    decisions_log: list[dict] = field(default_factory=list)
    rejects_log: list[dict] = field(default_factory=list)
    #: 预测复盘采样（D-055 批次3）：(PredictionRecord, 决策时 mid) 与 (ts, mid) 观测序列
    prediction_samples: list = field(default_factory=list)
    mid_observations: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StepConfig:
    """决策所需的最小配置（离线 MultiBacktestConfig 与实时 LiveConfig 都能提供）。"""

    decide_on: tuple[str, ...] = ("book_snapshot", "book_delta", "trade")


def build_commitment_provider(accounting: AccountingCore, tracker: OrderTracker):
    """P0017.2 组合承诺视图（与离线 driver 同一口径；离线/实时共享）。"""

    def provider():
        positions: dict[str, float] = {}
        unpriced: set[str] = set()
        for pos in accounting.positions:
            if pos.qty != 0.0 and pos.notional is None:
                unpriced.add(pos.symbol)
            elif pos.notional:
                positions[pos.symbol] = float(pos.notional)
        pending = tracker.exposure_by_symbol()
        unresolved: dict[str, bool] = {}
        for sym in set(positions) | set(pending) | set(unpriced):
            unresolved[sym] = (
                sym in unpriced
                or any(getattr(o, "symbol", None) in (None, sym)
                        for o in tracker.unresolved_orders()))
        net = {pos.symbol: float(pos.qty) for pos in accounting.positions}
        return PortfolioCommitment(
            position_by_symbol=positions,
            pending_by_symbol={s: float(v["total"]) for s, v in pending.items()},
            has_unresolved_by_symbol=unresolved), net

    return provider


def decide_and_submit(*, fn, engine: ExecutionEngine, tracker: OrderTracker, symbol: str,
                       state: MarketState, now: int, counters: StepCounters,
                       prediction=None) -> None:
    """执行一次 MakerPolicy 决策并把 PLACE 提交给共享引擎（离线/实时唯一实现）。

    风险拒绝不产生 manager 订单（engine.submit 在注册前拒绝），因此无成交/会计副作用。
    """
    snapshot = engine.snapshot(symbol, now_ms=now)
    position = engine.accounting.position(symbol)
    budget = None
    try:
        budget = remaining_exposure_budget(snapshot, engine.gate.limits).remaining_notional
    except Exception:  # noqa: BLE001 - 预算仅供策略参考；不可得时给 None（硬闸仍在引擎内）
        budget = None
    common = dict(state=state, prediction=prediction, position=position, snapshot=snapshot,
                  existing_orders=tuple(o for o in tracker.active() if o.symbol == symbol),
                  remaining_risk_budget=budget,
                  kill_switch=engine.gate.limits.effective_kill_switch_mode)
    decision = fn(**common) if callable(fn) and not hasattr(fn, "decide") else fn.decide(**common)
    if decision is None:
        return
    counters.decisions += 1
    counters.decisions_log.append({"ts": now, "symbol": symbol,
                                     "mode": (getattr(decision, "mode", None)
                                              and decision.mode.value),
                                     "decision_id": getattr(decision, "decision_id", None)})
    for side in (getattr(decision, "bid", None), getattr(decision, "ask", None)):
        if side is None:
            continue
        action = getattr(side, "action", None)
        if action in (QuoteAction.KEEP, QuoteAction.NONE, None):
            continue
        proposal = getattr(side, "proposal", None)
        if proposal is None:
            continue
        result = engine.submit(proposal, now_ms=now)
        if result.submitted:
            counters.submits += 1
        elif result.rejected:
            counters.risk_rejects += 1
            code = result.decision.reason_code
            counters.rejects_log.append(
                {"ts": now, "symbol": symbol,
                 "reason": code.value if code is not None else None})


def _settle_funding(engine: ExecutionEngine, trackers, symbol: str, now: int) -> None:
    tracker = None if trackers is None else trackers.get(symbol)
    if tracker is None:
        return
    qty = float(engine.accounting.position(symbol).qty)
    payment = tracker.settle_due(
        now_ms=now, position_qty=qty,
        mark_price=engine.accounting.mark_price(symbol))
    if payment is not None:
        engine.accounting.record_funding(payment)


def process_market_event(*, event: MarketEvent, now: int, engine: ExecutionEngine,
                          features: dict[str, FeatureEngine],
                          decision_fn_for: Callable[[str], object],
                          config: StepConfig, counters: StepCounters,
                          prediction_runtime=None,
                          funding_trackers: dict | None = None) -> MarketState | None:
    """处理**一个**市场事件的完整步进；返回该事件更新出的 MarketState（mark 事件为 None）。

    离线/实时共用。事件必须属于 `features` 中已配置的 symbol，否则 KeyError（调用方负责
    在边界把未知 symbol 转成明确失败，绝不静默丢弃）。
    `funding_trackers` 给出时（回测显式 opt-in），在 mark 分支登记费率并在越过结算点时结算。
    """
    symbol = event.symbol
    feature = features[symbol]  # 未知 symbol：显式抛出，由边界处理
    etype = event.event_type

    polled = engine.poll(now_ms=now)
    counters.fills += len(polled.fills)
    counters.events += 1

    state: MarketState | None = None
    if etype is EventType.MARK_PRICE:
        try:
            engine.accounting.update_mark_price(
                symbol, float(event.payload.price),
                timestamp=int(getattr(event.payload, "receive_ts", now)))
        except (AttributeError, TypeError, ValueError):
            pass
        tracker = None if funding_trackers is None else funding_trackers.get(symbol)
        if tracker is not None:
            tracker.update(
                funding_rate=getattr(event.payload, "funding_rate", None),
                next_funding_time=getattr(event.payload, "next_funding_time", None))
            _settle_funding(engine, funding_trackers, symbol, now)
    else:
        _settle_funding(engine, funding_trackers, symbol, now)
        state = feature.on_market_event(event)
        mid = getattr(getattr(state, "price", None), "mid", None)
        if mid is not None:
            counters.mid_observations.append((now, float(mid)))
        # 路由到该 symbol 独占的模拟 venue（经 adapter）；其他 venue 不可见
        engine.manager.adapter.on_market_event(event)
        if etype.value in config.decide_on and state is not None and state.quality.tradeable:
            fn = decision_fn_for(symbol)
            if fn is not None:
                prediction = invoke_provider_record(prediction_runtime, state, now)
                if prediction is not None and mid is not None:
                    counters.prediction_samples.append((prediction, float(mid)))
                decide_and_submit(fn=fn, engine=engine, tracker=engine.manager.tracker,
                                    symbol=symbol, state=state, now=now,
                                    counters=counters, prediction=prediction)
    return state


__all__ = [
    "StepCounters",
    "StepConfig",
    "build_commitment_provider",
    "decide_and_submit",
    "process_market_event",
]
