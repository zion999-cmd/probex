"""P0017.4：多标的确定性离线回测 driver。

在 P0017.3 单标的回测之上组合（不复制决策/风险/会计逻辑）：

- 每个 symbol：独立 `FeatureEngine`（市场可见状态隔离）+ 独立 `SimulatedVenue`（执行状态隔离）；
- 所有 symbol 共享：一个 `AccountingCore`、一个 `OrderTracker/OrderManager`、
  一个 `RiskGate`（含 P0017.2 组合 gross 硬闸，经 `MultiSymbolSimulatedAdapter` 提交）；
- 单一事件时间线：event-store 事件按 `(exchange_ts, ordinal/file-order, symbol)` 确定顺序处理；
- 决策按 symbol 逐个同步进行，同一引擎/组合预算；不使用线程/wall-clock。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from backtest import BacktestError, BacktestStatus
from backtest.driver import (
    BacktestConfig,
    BacktestTrace,
    BacktestResult,
    build_prediction_evaluation,
    run_integrity_checks,
    simulation_fee_schedule,
    simulation_slippage,
)
from backtest.multi_adapter import MultiSymbolSimulatedAdapter
from execution.engine import ExecutionEngine
from execution.manager import OrderManager
from execution.simulation.latency import LatencyModel
from execution.simulation.venue import SimulatedVenue
from execution.tracker import OrderTracker
from market.events.types import EventType, MarketEvent, Venue
from market.features.engine import FeatureEngine
from portfolio.accounting import AccountingCore
from portfolio.funding_model import FundingSettlementTracker
from risk.gate import RiskGate
from runtime.multi_symbol_core import (
    StepConfig,
    StepCounters,
    build_commitment_provider,
    process_market_event,
)
@dataclass
class MultiBacktestConfig(BacktestConfig):
    """多标的回测配置。`symbol` 为兼容字段；以 `symbols` 为准（必须显式、有序）。"""

    symbols: tuple[str, ...] = ()
    #: 每个 symbol 的 tick/step（P0017.4 v1：必须显式给出；缺省回退到父类单值以保持配置兼容）
    tick_size_by_symbol: dict[str, float] = field(default_factory=dict)
    quantity_step_by_symbol: dict[str, float] = field(default_factory=dict)

    def canonical(self) -> dict[str, object]:
        base = super().canonical()
        base["symbols"] = list(self.symbols)
        base["tick_size_by_symbol"] = dict(sorted(self.tick_size_by_symbol.items()))
        base["quantity_step_by_symbol"] = dict(sorted(self.quantity_step_by_symbol.items()))
        return base


def _ordered_events(records, *, declared_symbols: set[str]) -> list[tuple[int, int, str, MarketEvent]]:
    """统一事件排序：(exchange_ts, store ordinal, symbol)。

    - 同一 store 的 ordinal 保留来源顺序（同一时间戳跨标的的确定性 tiebreak）；
    - 校验 exchange_ts 非递减（跨标的合并后仍要求时间线单调）。
    """
    ordered: list[tuple[int, int, str, MarketEvent]] = []
    for ordinal, record in enumerate(records):
        event = record.event
        if not isinstance(event, MarketEvent):
            raise BacktestError(BacktestStatus.INVALID_INPUT,
                                  f"record {ordinal} is not a MarketEvent", location=str(ordinal))
        if event.symbol not in declared_symbols:
            raise BacktestError(
                BacktestStatus.INVALID_INPUT,
                f"record {ordinal} symbol {event.symbol!r} not in declared symbols "
                f"{sorted(declared_symbols)}; unknown symbols are not silently dropped",
                location=str(ordinal))
        ordered.append((int(event.exchange_ts), ordinal, event.symbol, event))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return ordered


def _build_commitment(accounting: AccountingCore, tracker: OrderTracker):
    """P0017.5：委托给共享核心（离线/实时同口径）。保留名字兼容既有导入/测试。"""
    return build_commitment_provider(accounting, tracker)


def run_multi_backtest(dataset_path: str | Path, config: MultiBacktestConfig, *,
                        run_id: str, dataset_sha256: str,
                        decision_fn=None, prediction_runtime=None) -> BacktestResult:
    """执行一次多标的确定性回测。decision_fn 是 symbol→决策函数的映射（或单一函数用于全部 symbol）。"""
    path = Path(dataset_path)

    symbols = tuple(config.symbols)
    if not symbols or len(symbols) < 2:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              "multi-symbol backtest requires >=2 declared symbols")
    declared = set(symbols)
    if len(declared) != len(symbols):
        raise BacktestError(BacktestStatus.INVALID_INPUT, "duplicate symbols in config")

    from backtest.dataset_source import load_records

    records = load_records(path, symbols=symbols)
    if not records:
        raise BacktestError(BacktestStatus.INVALID_INPUT, "dataset is empty")
    ordered = _ordered_events(records, declared_symbols=declared)

    # 校验合并后时间线（exchange_ts 已由排序保证非递减；显式拒绝严格回退）
    last_ts = -1
    for ts, _ordinal, _symbol, _event in ordered:
        if ts < last_ts:
            raise BacktestError(BacktestStatus.INVALID_INPUT,
                                  "event timeline goes backwards after ordering")
        last_ts = ts

    accounting = AccountingCore(initial_balance=float(config.initial_balance))
    tracker = OrderTracker(session_id=run_id)

    venues: dict[str, SimulatedVenue] = {}
    engines_feature: dict[str, FeatureEngine] = {}
    for symbol in symbols:
        venues[symbol] = SimulatedVenue(
            symbol=symbol,
            latency=LatencyModel(submit_latency_ms=int(config.submit_latency_ms),
                                   cancel_latency_ms=int(config.cancel_latency_ms)),
            fee_schedule=simulation_fee_schedule(config),
            slippage=simulation_slippage(config),
            venue=Venue.BINANCE)
        engines_feature[symbol] = FeatureEngine(venue=Venue.BINANCE, symbol=symbol,
                                                   max_book_age_ms=config.max_book_age_ms)

    adapter = MultiSymbolSimulatedAdapter(venues)
    manager = OrderManager(tracker=tracker, adapter=adapter)
    limits = config.to_limits()
    engine = ExecutionEngine(
        accounting=accounting, gate=RiskGate(limits), manager=manager,
        portfolio_commitment_provider=_build_commitment(accounting, tracker))

    step_config = StepConfig(decide_on=tuple(config.decide_on))
    counters = StepCounters()
    funding_trackers = (
        {symbol: FundingSettlementTracker(asset=config.fee_asset, symbol=symbol)
         for symbol in symbols}
        if getattr(config, "funding_enabled", False) else None)

    def fn_for_symbol(symbol: str):
        if decision_fn is None:
            return None
        if isinstance(decision_fn, dict):
            return decision_fn.get(symbol)
        return decision_fn  # 单一函数用于全部 symbol

    try:
        for ts, _ordinal, symbol, event in ordered:
            now = int(ts)
            # P0017.5：每个事件的决策/风险/执行/会计步进走离线/实时共享核心（唯一实现）。
            process_market_event(
                event=event, now=now, engine=engine, features=engines_feature,
                decision_fn_for=fn_for_symbol, config=step_config, counters=counters,
                prediction_runtime=prediction_runtime,
                funding_trackers=funding_trackers)
        if funding_trackers is not None:
            for symbol, tracker in funding_trackers.items():
                payment = tracker.settle_due(
                    now_ms=int(ordered[-1][0]),
                    position_qty=float(accounting.position(symbol).qty),
                    mark_price=accounting.mark_price(symbol))
                if payment is not None:
                    accounting.record_funding(payment)
        tail = engine.poll(now_ms=int(ordered[-1][0]))
        counters.fills += len(tail.fills)
    except BacktestError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BacktestError(BacktestStatus.REPLAY_FAILURE,
                              f"{type(exc).__name__}: {exc}") from exc

    # 共享计数器 → 回测 trace（保持 BacktestResult 契约不变）
    trace = BacktestTrace(
        decisions=counters.decisions, submits=counters.submits,
        risk_rejects=counters.risk_rejects, fills=counters.fills,
        events=counters.events, decisions_log=counters.decisions_log,
        rejects_log=counters.rejects_log)

    evaluation = (
        build_prediction_evaluation(
            config, counters.prediction_samples, counters.mid_observations)
        if counters.prediction_samples else None)
    result = BacktestResult(
        status=BacktestStatus.SUCCESS, run_id=run_id, config=config, trace=trace,
        accounting=accounting, tracker=tracker, engine=engine,
        dataset_sha256=dataset_sha256, prediction_evaluation=evaluation)
    checks = run_integrity_checks(result)
    # 额外：多标的守恒与覆盖检查
    checks.extend(_multi_integrity_checks(result, symbols, seen_symbols=_seen(ordered)))
    result.integrity_checks = checks
    failed = [c for c in checks if not c["passed"]]
    if failed:
        result.status = BacktestStatus.INTEGRITY_FAILURE
        result.reason = "; ".join(c["name"] for c in failed)
    return result


def _seen(ordered) -> set[str]:
    return {symbol for _ts, _o, symbol, _e in ordered}


def _decide_multi(fn, engine: ExecutionEngine, tracker: OrderTracker, symbol: str,
                   state, config: BacktestConfig, now: int, trace: BacktestTrace, *,
                   prediction=None) -> None:
    """P0017.5：委托给离线/实时共享决策步进（保留名字兼容既有导入/测试）。"""
    from runtime.multi_symbol_core import StepCounters, decide_and_submit, StepConfig
    counters = StepCounters()
    decide_and_submit(fn=fn, engine=engine, tracker=tracker, symbol=symbol, state=state,
                        now=now, counters=counters, prediction=prediction)
    trace.decisions += counters.decisions
    trace.submits += counters.submits
    trace.risk_rejects += counters.risk_rejects
    trace.decisions_log.extend(counters.decisions_log)
    trace.rejects_log.extend(counters.rejects_log)


def _multi_integrity_checks(result: BacktestResult, symbols: tuple[str, ...], *,
                              seen_symbols: set[str]) -> list[dict]:
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    tracker = result.tracker
    # 1) 在途暴露守恒：Σ by-symbol == total
    by = tracker.exposure_by_symbol()
    summed = sum(v["total"] for v in by.values())
    record("multi_pending_conservation",
           abs(summed - tracker.total_pending_exposure()) < 1e-9,
           f"sum={summed} total={tracker.total_pending_exposure()}")
    # 2) 每个声明且出现过的 symbol 都有独立执行/状态（订单归属正确）
    order_symbols = {o.symbol for o in tracker.orders}
    unknown = order_symbols - set(symbols)
    record("all_orders_belong_to_declared_symbols", not unknown,
           f"unknown={sorted(unknown)}")
    # 3) 声明的 symbol 集合 ∩ 数据集非空（避免静默只跑了部分标的）
    missing = [s for s in symbols if s not in seen_symbols]
    record("every_declared_symbol_present_in_data", not missing,
           f"declared_but_absent={missing}")
    # 4) 组合 gross = Σ 各 symbol 持仓名义（与 accounting 一致）
    gross = result.accounting.gross_exposure
    computed = sum(p.notional or 0.0 for p in result.accounting.positions)
    record("portfolio_gross_equals_sum_symbol_notionals",
           gross is not None and abs(gross - computed) < 1e-6,
           f"gross={gross} sum={computed}")
    return checks


__all__ = ["MultiBacktestConfig", "run_multi_backtest"]
