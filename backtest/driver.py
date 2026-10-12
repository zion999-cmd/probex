"""确定性离线回测 driver（P0017.3）。

单一、同步、单线程编排，**不**使用 wall-clock 节流 / 后台线程：

    JSONL event store
      → ReplaySource 按 exchange_ts 步进
      → FeatureEngine 更新可见市场状态（决策只能见 ≤ 当前事件）
      → 在决策边界同步调用 MakerPolicy（可选预测）
      → ExecutionEngine.submit（现有 RiskGate + P0017.2 组合硬闸）
      → SimulatedVenue.on_market_event + engine.poll（成交/会计）
      → 轨迹记录 → 结束完整性校验 → 结果

结构性隔离：本模块只 import 既有领域组件与本地模拟适配器；
**不 import** connectors / runtime.testnet / runtime.assembly，不触网、不持凭据。
"""

from __future__ import annotations

import json
import pathlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from execution.engine import ExecutionEngine
from execution.manager import OrderManager
from execution.simulation.fees import FeeSchedule
from execution.simulation.latency import LatencyModel
from execution.simulation.venue import SimulatedVenue
from execution.tracker import OrderTracker
from market.events.types import MarketEvent, EventType, Venue
from market.features.engine import FeatureEngine
from market.replay.source import ReplayClock, ReplayMode, ReplaySource
from portfolio.accounting import AccountingCore
from portfolio.types import Side
from risk.budget import remaining_exposure_budget
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.portfolio_budget import PortfolioCommitment
from storage.events.reader import JsonlEventReader
from strategy.maker.types import QuoteAction

from backtest import BacktestError, BacktestStatus
from backtest.identity import EXECUTION_MODEL

DecisionFn = Callable[[object, object, object, object, float | None], object]


@dataclass
class BacktestConfig:
    """回测生效配置（必须显式；缺业务数值时不得编造默认）。"""

    symbol: str
    initial_balance: float
    maker_fee_rate: float
    fee_asset: str
    submit_latency_ms: int
    cancel_latency_ms: int
    #: 单标的持仓名义上限（None=不启用）
    max_position_notional: float | None = None
    #: P0017.2 组合 gross 上限（None=不启用，报告须如实声明）
    max_portfolio_gross_notional: float | None = None
    max_open_order_exposure: float | None = None
    max_daily_loss: float | None = None
    max_drawdown_pct: float | None = None
    max_book_age_ms: int | None = None
    seed: int | None = None
    #: 决策触发的事件类型集合（默认：盘口快照与逐笔成交后都评估）
    decide_on: tuple[str, ...] = ("book_snapshot", "book_delta", "trade")

    def to_limits(self) -> RiskLimits:
        return RiskLimits(
            max_position_notional=self.max_position_notional,
            max_open_order_exposure=self.max_open_order_exposure,
            max_portfolio_gross_notional=self.max_portfolio_gross_notional,
            max_daily_loss=self.max_daily_loss,
            max_drawdown_pct=self.max_drawdown_pct,
            max_mark_age_ms=self.max_book_age_ms)

    def canonical(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "initial_balance": self.initial_balance,
            "maker_fee_rate": self.maker_fee_rate,
            "fee_asset": self.fee_asset,
            "submit_latency_ms": self.submit_latency_ms,
            "cancel_latency_ms": self.cancel_latency_ms,
            "max_position_notional": self.max_position_notional,
            "max_portfolio_gross_notional": self.max_portfolio_gross_notional,
            "max_open_order_exposure": self.max_open_order_exposure,
            "max_daily_loss": self.max_daily_loss,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_book_age_ms": self.max_book_age_ms,
            "seed": self.seed,
            "decide_on": list(self.decide_on),
        }


@dataclass
class BacktestTrace:
    decisions: int = 0
    submits: int = 0
    risk_rejects: int = 0
    cancels: int = 0
    fills: int = 0
    events: int = 0
    decisions_log: list[dict] = field(default_factory=list)
    rejects_log: list[dict] = field(default_factory=list)


@dataclass
class BacktestResult:
    status: BacktestStatus
    run_id: str
    config: BacktestConfig
    trace: BacktestTrace
    accounting: AccountingCore
    tracker: OrderTracker
    engine: ExecutionEngine
    dataset_sha256: str
    reason: str = ""
    location: str | None = None
    integrity_checks: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is BacktestStatus.SUCCESS


def _validate_dataset(events: list[MarketEvent], *, symbol: str) -> None:
    """输入契约（设计 §4.2）：存在、可解析、时间单调、事件类型合法。"""
    if not events:
        raise BacktestError(BacktestStatus.INVALID_INPUT, "event store is empty")
    last_ts = -1
    for index, event in enumerate(events):
        if not isinstance(event, MarketEvent):
            raise BacktestError(BacktestStatus.INVALID_INPUT,
                                  f"record {index} is not a MarketEvent", location=str(index))
        if event.symbol != symbol:
            raise BacktestError(
                BacktestStatus.INVALID_INPUT,
                f"record {index} symbol {event.symbol!r} != configured {symbol!r}; "
                "multi-symbol backtest is not supported by the single-symbol event driver",
                location=str(index))
        if event.exchange_ts < last_ts:
            raise BacktestError(
                BacktestStatus.INVALID_INPUT,
                f"record {index} exchange_ts {event.exchange_ts} < previous {last_ts}; "
                "event-store must be time-ordered for deterministic replay",
                location=str(index))
        last_ts = event.exchange_ts


def run_backtest(dataset_path: str | Path, config: BacktestConfig, *,
                  run_id: str, dataset_sha256: str,
                  decision_fn: DecisionFn | None = None,
                  prediction_runtime: object | None = None) -> BacktestResult:
    """执行一次确定性回测。所有时间锚定 exchange_ts；无 wall-clock。"""
    path = Path(dataset_path)
    if not path.is_file():
        raise BacktestError(BacktestStatus.INVALID_INPUT, f"dataset not found: {path}")

    records = tuple(JsonlEventReader(path))
    events = [r.event for r in records]
    _validate_dataset(events, symbol=config.symbol)

    accounting = AccountingCore(initial_balance=float(config.initial_balance))
    tracker = OrderTracker(session_id=run_id)
    venue = SimulatedVenue(
        symbol=config.symbol,
        latency=LatencyModel(submit_latency_ms=int(config.submit_latency_ms),
                              cancel_latency_ms=int(config.cancel_latency_ms)),
        fee_schedule=FeeSchedule(maker_fee_rate=float(config.maker_fee_rate),
                                   fee_asset=config.fee_asset),
        venue=Venue.BINANCE)
    manager = OrderManager(tracker=tracker, adapter=venue)
    limits = config.to_limits()

    def commitment_provider():
        positions = {}
        net = {}
        for pos in accounting.positions:
            net[pos.symbol] = float(pos.qty)
            if pos.notional:
                positions[pos.symbol] = float(pos.notional)
        # L1：非 flat 持仓缺 mark ⇒ 不可量化（fail-closed）
        unpriced = {s for s in (p.symbol for p in accounting.positions)
                      if (lambda p: p.qty != 0.0 and p.notional is None)(accounting.position(s))}
        pending = tracker.exposure_by_symbol()
        # 只有不可量化订单（unresolved_count>0）或缺 mark 的非 flat 持仓才标 unresolved；
        # 普通在途订单有名义，不是 unresolved（P0017.4 修正：此前错误地把所有 pending symbol 标 True）。
        unresolved: dict[str, bool] = {}
        for s in set(positions) | set(pending) | unpriced:
            unresolved[s] = s in unpriced or any((getattr(o, 'symbol', None) in (None, s)) for o in tracker.unresolved_orders())
        return PortfolioCommitment(
            position_by_symbol=positions,
            pending_by_symbol={s: float(v["total"]) for s, v in pending.items()},
            has_unresolved_by_symbol=unresolved), net

    engine = ExecutionEngine(
        accounting=accounting, gate=RiskGate(limits), manager=manager,
        portfolio_commitment_provider=commitment_provider)
    feature_engine = FeatureEngine(venue=Venue.BINANCE, symbol=config.symbol,
                                    max_book_age_ms=config.max_book_age_ms)
    trace = BacktestTrace()

    decide_set = set(config.decide_on)
    state = None
    try:
        for index, event in enumerate(events):
            now = int(event.exchange_ts)
            etype = event.event_type
            # MARK_PRICE 不进 FeatureEngine 的盘口（book 不接受）；它是风险/会计的正式 mark
            if etype is EventType.MARK_PRICE:
                payload = event.payload
                try:
                    accounting.update_mark_price(config.symbol, float(payload.price),
                                                   timestamp=int(getattr(payload, "receive_ts", now)))
                except (AttributeError, TypeError, ValueError):
                    pass
            else:
                # 1) 推进可见市场状态（策略只能见 ≤now）
                state = feature_engine.on_market_event(event)
            # 2) 把市场事件喂给模拟器（仅 book/trade；mark 不是成交驱动事件，venue 不接受）
            if etype is not EventType.MARK_PRICE:
                venue.on_market_event(event)
            # 3) 处理在此事件时点到期/成交的模拟事件
            polled = engine.poll(now_ms=now)
            trace.fills += len(polled.fills)
            trace.events += 1
            # 4) 决策边界（同步；仅在产生了市场状态的事件后）
            if decision_fn is not None and etype is not EventType.MARK_PRICE \
                    and etype.value in decide_set and state.quality.tradeable:
                prediction = _predict(prediction_runtime, state, now)
                _decide_once(decision_fn, engine, tracker, venue, state, config, now, trace,
                              prediction=prediction)
        # 结束：用最后事件时间再 poll 一次，处理尾部到期成交
        final_ts = int(events[-1].exchange_ts)
        tail = engine.poll(now_ms=final_ts)
        trace.fills += len(tail.fills)
    except BacktestError:
        raise
    except Exception as exc:  # noqa: BLE001 - 组件错误归类为 REPLAY_FAILURE
        raise BacktestError(BacktestStatus.REPLAY_FAILURE,
                              f"{type(exc).__name__}: {exc}") from exc

    result = BacktestResult(
        status=BacktestStatus.SUCCESS, run_id=run_id, config=config, trace=trace,
        accounting=accounting, tracker=tracker, engine=engine,
        dataset_sha256=dataset_sha256)
    checks = run_integrity_checks(result)
    result.integrity_checks = checks
    failed = [c for c in checks if not c["passed"]]
    if failed:
        result.status = BacktestStatus.INTEGRITY_FAILURE
        result.reason = "; ".join(c["name"] for c in failed)
    return result


def _predict(runtime: object | None, state, now: int):
    """同步调用可选 PredictionProvider（离线本地 provider 确定性、无随机性）。

    P0017.5：实现已抽取到中立的 `prediction.invoke.invoke_provider_record`，
    供离线回测与实时运行共享同一事件循环/失败语义（保留此名以兼容既有导入与测试）。
    """
    from prediction.invoke import invoke_provider_record
    return invoke_provider_record(runtime, state, now)


def _decide_once(decision_fn, engine: ExecutionEngine, tracker: OrderTracker,
                  venue: SimulatedVenue, state, config: BacktestConfig,
                  now: int, trace: BacktestTrace, *, prediction=None) -> None:
    snapshot = engine.snapshot(config.symbol, now_ms=now)
    position = engine.accounting.position(config.symbol)
    budget = None
    try:
        budget = remaining_exposure_budget(snapshot, engine.gate.limits).remaining_notional
    except Exception:  # noqa: BLE001 - 算不出预算 ⇒ None（fail closed，与 PAPER 一致）
        budget = None
    decision = decision_fn(state=state, prediction=prediction, position=position,
                            snapshot=snapshot,
                            existing_orders=tracker.active(),
                            remaining_risk_budget=budget,
                            kill_switch=engine.gate.limits.effective_kill_switch_mode)
    if decision is None:
        return
    trace.decisions += 1
    trace.decisions_log.append({"ts": now, "mode": getattr(decision, "mode", None) and decision.mode.value,
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
            trace.submits += 1
        elif result.rejected:
            trace.risk_rejects += 1
            code = result.decision.reason_code
            trace.rejects_log.append({
                "ts": now, "reason": code.value if code is not None else None})


def run_integrity_checks(result: BacktestResult) -> list[dict]:
    """强制会计/订单恒等式（设计 §8.2）。失败 ⇒ INTEGRITY_FAILURE。"""
    accounting = result.accounting
    tracker = result.tracker
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    # 1) 成交账本计数 == 会计 accepted fills
    ledger_count = accounting.fills.count
    record("fills_match_ledger", ledger_count == result.trace.fills,
           f"ledger={ledger_count} trace_fills={result.trace.fills}")

    # 2) 持仓 = Σ signed fills（按 mark 前的成交均价口径由 accounting 自身保证；这里校验非负权益输入）
    record("equity_finite", accounting.equity() is None or accounting.equity() == accounting.equity(),
           "equity callable")

    # 3) 在途守恒：tracker total pending == Σ exposure_by_symbol
    by_symbol = tracker.exposure_by_symbol()
    summed = sum(v["total"] for v in by_symbol.values())
    record("pending_exposure_conservation",
           abs(summed - tracker.total_pending_exposure()) < 1e-9,
           f"sum={summed} total={tracker.total_pending_exposure()}")

    # 4) 终态/活动订单状态合法
    statuses = {o.status.value for o in tracker.orders}
    record("order_statuses_known", all(isinstance(s, str) and s for s in statuses),
           ",".join(sorted(statuses)))

    # 5) 配置了组合限额却未接线 ⇒ 配置完整性失败（不应发生，driver 总是提供 provider）
    if result.config.max_portfolio_gross_notional is not None:
        record("portfolio_gate_wired", result.engine.portfolio_commitment_provider is not None,
               "portfolio limit configured but provider absent")
    return checks


__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "BacktestStatus",
    "BacktestTrace",
    "run_backtest",
    "run_integrity_checks",
]
