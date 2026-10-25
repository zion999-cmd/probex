"""Bar OHLCV 回测引擎（与 event-driven 引擎并存；人类 2026-10-24 授权双模式）。

输入：仅含 `EventType.BAR` 的事件序列（Event Store 或共享数据集 `bar` kind）。
不构建 LOB 特征；决策函数直接收到 BarPayload（bar-only 研究不能伪装成盘口状态）。

两种成交模式（`backtest.bar_mode`）：
- `limit_conservative`：post-only 限价挂单下一根 bar 按 high/low 触碰判定，同根同时碰到
  高低价 ⇒ 保守不成交；maker 费率；
- `close_market`：收盘决策的非 post-only 单在下一根 open 经固定 bps 滑点成交，taker 费率。

复用唯一会计/风险链：ExecutionEngine + RiskGate + OrderTracker + AccountingCore
（bar 适配器只负责成交判定，不持有账户真值）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from backtest import BacktestError, BacktestStatus
from backtest.driver import (
    BacktestConfig,
    BacktestResult,
    BacktestTrace,
    run_integrity_checks,
    simulation_fee_schedule,
    simulation_slippage,
)
from execution.engine import ExecutionEngine
from execution.manager import OrderManager
from execution.simulation.bar_adapter import (
    BAR_EXECUTION_MODES,
    CLOSE_MARKET,
    LIMIT_CONSERVATIVE,
    BarExecutionAdapter,
)
from execution.tracker import OrderTracker
from market.events.payloads import BarPayload
from market.events.types import EventType, MarketEvent
from portfolio.accounting import AccountingCore
from risk.gate import RiskGate
from risk.types import OrderProposal

BarDecisionFn = Callable[[BarPayload, object], tuple[OrderProposal, ...] | None]


@dataclass
class BarBacktestConfig(BacktestConfig):
    """bar 回测配置（继承限额/资金费等；`bar_mode` 必填）。"""

    bar_mode: str = LIMIT_CONSERVATIVE

    def canonical(self) -> dict[str, object]:
        base = super().canonical()
        base["bar_mode"] = self.bar_mode
        base["execution_granularity"] = "bar"
        return base


def _bars_from_records(records, *, symbol: str) -> list[BarPayload]:
    bars: list[BarPayload] = []
    last_close_ts = -1
    for ordinal, record in enumerate(records):
        event = record.event
        if not isinstance(event, MarketEvent):
            raise BacktestError(BacktestStatus.INVALID_INPUT,
                                  f"record {ordinal} is not a MarketEvent")
        if event.symbol != symbol:
            raise BacktestError(BacktestStatus.INVALID_INPUT,
                                  f"record {ordinal} symbol {event.symbol!r} != {symbol!r}")
        if event.event_type is not EventType.BAR:
            raise BacktestError(
                BacktestStatus.INVALID_INPUT,
                f"bar backtest accepts only BAR events, got {event.event_type.value!r} "
                "(mixing LOB/trade events belongs to the event-driven engine)")
        payload = event.payload
        if not isinstance(payload, BarPayload):
            raise BacktestError(BacktestStatus.INVALID_INPUT,
                                  f"record {ordinal} BAR event has no BarPayload")
        if payload.close_ts < last_close_ts:
            raise BacktestError(BacktestStatus.INVALID_INPUT, "bars are not in chronological order")
        last_close_ts = payload.close_ts
        bars.append(payload)
    return bars


def run_bar_backtest(dataset_path: str | Path, config: BarBacktestConfig, *,
                      run_id: str, dataset_sha256: str,
                      decision_fn: BarDecisionFn | None = None) -> BacktestResult:
    """执行一次确定性 bar 回测。决策在每根 bar 收盘时做出，成交在下一根 bar 判定。"""
    from backtest.dataset_source import load_records

    if config.bar_mode not in BAR_EXECUTION_MODES:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"unknown bar_mode {config.bar_mode!r}")
    records = load_records(Path(dataset_path), symbols=(config.symbol,))
    if not records:
        raise BacktestError(BacktestStatus.INVALID_INPUT, "dataset is empty")
    bars = _bars_from_records(records, symbol=config.symbol)
    if not bars:
        raise BacktestError(BacktestStatus.INVALID_INPUT, "no BAR events in dataset")

    accounting = AccountingCore(initial_balance=float(config.initial_balance))
    tracker = OrderTracker(session_id=run_id)
    fees = simulation_fee_schedule(config)
    slippage = simulation_slippage(config)
    adapter = BarExecutionAdapter(
        symbol=config.symbol, mode=config.bar_mode, fee_schedule=fees,
        slippage=(slippage if config.bar_mode == CLOSE_MARKET else None))
    manager = OrderManager(tracker=tracker, adapter=adapter)
    engine = ExecutionEngine(
        accounting=accounting, gate=RiskGate(config.to_limits()),
        manager=manager, book_healthy=True)

    trace = BacktestTrace()
    try:
        for bar in bars:
            now = int(bar.close_ts)
            # 风险/会计 mark 用收盘价（bar 引擎唯一可用的正式价格）
            accounting.update_mark_price(config.symbol, float(bar.close), timestamp=now)
            # 先判定上一根挂单/市价单的成交（不使用当根决策信息）
            adapter.on_bar(bar)
            polled = engine.poll(now_ms=now)
            trace.fills += len(polled.fills)
            # 收盘决策（只能见已收盘的本根及之前）
            if decision_fn is not None:
                proposals = decision_fn(bar, engine.snapshot(config.symbol, now_ms=now)) or ()
                for proposal in proposals:
                    result = engine.submit(proposal, now_ms=now)
                    trace.submits += 1 if result.submitted else 0
                    if result.rejected:
                        trace.risk_rejects += 1
                trace.decisions += 1
            trace.events += 1
    except BacktestError:
        raise
    except Exception as exc:  # noqa: BLE001
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


__all__ = [
    "CLOSE_MARKET",
    "LIMIT_CONSERVATIVE",
    "BarBacktestConfig",
    "run_bar_backtest",
]
