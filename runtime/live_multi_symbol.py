"""P0017.5：实时多标的行情驱动的**模拟**交易运行编排（L2）。

唯一职责：把实时多标的采集批次，持续交给 `runtime.multi_symbol_core` 的共享步进，
驱动"行情→per-symbol 状态/决策→共享组合风险（P0017.2）→多个 SimulatedVenue→会计"闭环。

强约束（设计 §3.4 / §4-B2）：

- **只模拟**：执行适配器固定为 `MultiSymbolSimulatedAdapter`（per-symbol SimulatedVenue）。
  本模块**不得** import `runtime.testnet` 或任何 binance 写执行 connector；模拟模式不可能因
  配置/注入错误降级为真实下单（结构性隔离，另有 AST 守卫测试）。
- **单线程确定性步进**：`run_forever` 在一个线程内同步 pump→process，不并发提交；
  同一批次内按 `(exchange_ts, symbol, arrival)` 排序，预算竞争的胜出确定。
- **不使用未来事件**：维护 per-symbol 单调时间门，乱序（exchange_ts 回退）事件**跳过并计数**，
  绝不喂给决策；重复（ts 与事件内容相同）事件计数并按策略跳过，不静默丢弃关键事件。
- **停止**：禁新增 → 撤销所有 active 模拟挂单 → poll 确认终态；**不**虚构收盘价、**不**强平、
  **不**把模拟成交伪装成真实；撤单后仍 active 的订单如实报告（不假定撤销成功）。
- **故障隔离**：单个 symbol 的采集/处理异常被捕获、计数、标记该 symbol，其余 symbol 继续。

实时源通过 `BatchSource` 协议注入：生产用 `CollectorBatchSource`（包 MultiSymbolMarketCollector），
验收用脚本化 source（确定性，不依赖网络）。本模块本身不建网络连接。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from execution.engine import ExecutionEngine
from execution.manager import OrderManager
from execution.simulation.fees import FeeSchedule
from execution.simulation.latency import LatencyModel
from execution.simulation.venue import SimulatedVenue
from execution.tracker import OrderTracker
from market.events.types import MarketEvent, Venue
from market.features.engine import FeatureEngine
from portfolio.accounting import AccountingCore
from risk.gate import RiskGate
from runtime.multi_symbol_core import (
    StepConfig,
    StepCounters,
    build_commitment_provider,
    process_market_event,
)
from backtest.multi_adapter import MultiSymbolSimulatedAdapter


class LiveRunError(ValueError):
    """实时多标的模拟运行的输入/配置错误（明确失败，不静默继续）。"""


class RunStatus(str, Enum):
    INITIALIZED = "INITIALIZED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SymbolBatch:
    """一个 symbol 在一次 pump 中采集到的内容（采集器与脚本化源的共同输出形状）。"""

    symbol: str
    events: tuple[MarketEvent, ...] = ()
    source_error: str | None = None  # 该 symbol 本轮采集失败（故障隔离，不致命全局）


class BatchSource(Protocol):
    """实时批次来源。返回每个 symbol 一轮的批次；允许空批次（无新事件）。"""

    @property
    def symbols(self) -> tuple[str, ...]: ...

    def pump(self) -> tuple[SymbolBatch, ...]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LiveRiskConfig:
    """实时模拟运行的风险/会计配置（必须显式给出业务数值；不编造默认）。"""

    initial_balance: float
    maker_fee_rate: float
    fee_asset: str
    submit_latency_ms: int = 0
    cancel_latency_ms: int = 0
    max_position_notional: float | None = None
    max_portfolio_gross_notional: float | None = None
    max_open_order_exposure: float | None = None
    max_daily_loss: float | None = None
    max_drawdown_pct: float | None = None
    max_book_age_ms: int | None = 5_000
    decide_on: tuple[str, ...] = ("book_snapshot", "book_delta", "trade")

    def __post_init__(self) -> None:
        if self.initial_balance <= 0:
            raise LiveRunError("initial_balance must be > 0")
        if not self.fee_asset:
            raise LiveRunError("fee_asset required")


@dataclass
class SymbolHealth:
    healthy: bool = True
    error_count: int = 0
    last_error: str | None = None
    events_seen: int = 0
    out_of_order_skipped: int = 0
    duplicate_skipped: int = 0
    last_exchange_ts: int | None = None
    last_event_key: tuple | None = None
    #: 本运行内是否曾发生采集/处理故障（粘滞，供可观察；恢复后 healthy 可重新为 True）
    had_failure: bool = False

    def record_error(self, message: str) -> None:
        self.healthy = False
        self.had_failure = True
        self.error_count += 1
        self.last_error = message


@dataclass
class LiveRunStats:
    status: RunStatus = RunStatus.INITIALIZED
    pumps: int = 0
    counters: StepCounters = field(default_factory=StepCounters)
    health: dict[str, SymbolHealth] = field(default_factory=dict)
    unknown_symbol_events: int = 0
    stop_reason: str | None = None
    open_after_stop: tuple[str, ...] = ()
    stop_cancel_requested: int = 0
    stop_cancel_confirmed: int = 0

    def view(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "pumps": self.pumps,
            "decisions": self.counters.decisions,
            "submits": self.counters.submits,
            "risk_rejects": self.counters.risk_rejects,
            "fills": self.counters.fills,
            "events": self.counters.events,
            "unknown_symbol_events": self.unknown_symbol_events,
            "open_after_stop": list(self.open_after_stop),
            "stop_cancel_requested": self.stop_cancel_requested,
            "stop_cancel_confirmed": self.stop_cancel_confirmed,
            "stop_reason": self.stop_reason,
            "symbols": {s: h.__dict__.copy() for s, h in self.health.items()},
        }


class LiveMultiSymbolRunner:
    """实时多标的模拟交易 runner（单线程步进；模拟适配器固定，无真实下单能力）。"""

    def __init__(self, *, symbols: tuple[str, ...], source: BatchSource,
                  risk: LiveRiskConfig, decision_fn_for: Callable[[str], object],
                  prediction_runtime: object | None = None,
                  run_id: str = "live-paper",
                  tick_size_by_symbol: dict[str, float] | None = None,
                  quantity_step_by_symbol: dict[str, float] | None = None,
                  max_pumps: int | None = None) -> None:
        syms = tuple(symbols)
        if len(syms) < 2:
            raise LiveRunError("real-time multi-symbol run requires >=2 symbols")
        if len(set(syms)) != len(syms):
            raise LiveRunError("duplicate symbols")
        unknown = [s for s in syms if s not in source.symbols]
        if unknown:
            raise LiveRunError(f"symbols not provided by batch source: {unknown}")
        self._symbols = syms
        self._source = source
        self._risk = risk
        self._fn_for = decision_fn_for
        self._prediction = prediction_runtime
        self._max_pumps = max_pumps
        self._stop = threading.Event()
        self.stats = LiveRunStats(health={s: SymbolHealth() for s in syms})

        # ---- 组合拥有者（与离线多标的回测同一组装方式）----
        self._accounting = AccountingCore(initial_balance=float(risk.initial_balance))
        self._tracker = OrderTracker(session_id=run_id)
        venues: dict[str, SimulatedVenue] = {}
        self._features: dict[str, FeatureEngine] = {}
        for s in syms:
            venues[s] = SimulatedVenue(
                symbol=s,
                latency=LatencyModel(submit_latency_ms=int(risk.submit_latency_ms),
                                       cancel_latency_ms=int(risk.cancel_latency_ms)),
                fee_schedule=FeeSchedule(maker_fee_rate=float(risk.maker_fee_rate),
                                           fee_asset=risk.fee_asset),
                venue=Venue.BINANCE)
            self._features[s] = FeatureEngine(venue=Venue.BINANCE, symbol=s,
                                                max_book_age_ms=risk.max_book_age_ms)
        self._adapter = MultiSymbolSimulatedAdapter(venues)  # 固定模拟适配器
        self._manager = OrderManager(tracker=self._tracker, adapter=self._adapter)
        from risk.limits import RiskLimits
        limits = RiskLimits(
            max_position_notional=risk.max_position_notional,
            max_open_order_exposure=risk.max_open_order_exposure,
            max_portfolio_gross_notional=risk.max_portfolio_gross_notional,
            max_daily_loss=risk.max_daily_loss,
            max_drawdown_pct=risk.max_drawdown_pct,
            max_mark_age_ms=risk.max_book_age_ms)
        self._engine = ExecutionEngine(
            accounting=self._accounting, gate=RiskGate(limits), manager=self._manager,
            portfolio_commitment_provider=build_commitment_provider(
                self._accounting, self._tracker))
        self._step_config = StepConfig(decide_on=tuple(risk.decide_on))
        self.stats.status = RunStatus.INITIALIZED

    # ------------------------------------------------------------------ 只读句柄
    @property
    def engine(self) -> ExecutionEngine:
        return self._engine

    @property
    def accounting(self) -> AccountingCore:
        return self._accounting

    @property
    def tracker(self) -> OrderTracker:
        return self._tracker

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    # ------------------------------------------------------------------ 生命周期
    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self, *, pump_interval_s: float = 0.5) -> LiveRunStats:
        """持续 pump 直到 `request_stop()`/源关闭/达到 max_pumps（测试用）。"""
        if self.stats.status is RunStatus.RUNNING:
            raise LiveRunError("runner already running")
        self.stats.status = RunStatus.RUNNING
        self.stats.stop_reason = None
        pumps = 0
        try:
            while not self._stop.is_set():
                self._pump_once()
                pumps += 1
                if self._max_pumps is not None and pumps >= self._max_pumps:
                    self.stats.stop_reason = "max_pumps_reached"
                    break
                if self._max_pumps is None:
                    self._stop.wait(pump_interval_s)
                # 有限 pump（测试/注入式验收）：不空转等待，立即进入下一泵
        except Exception as exc:  # noqa: BLE001 - 全局致命错误如实标记（per-symbol 错误已隔离）
            self.stats.status = RunStatus.FAILED
            self.stats.stop_reason = f"{type(exc).__name__}: {exc}"
            return self.stats
        self._shutdown()
        return self.stats

    # ------------------------------------------------------------------ 单轮
    def _pump_once(self) -> None:
        batches = self._source.pump()
        self.stats.pumps += 1
        # 展平 + 确定性排序：(exchange_ts, symbol, arrival)
        flat: list[tuple[int, int, str, MarketEvent]] = []
        arrival = 0
        for batch in batches:
            health = self.stats.health.get(batch.symbol)
            if health is None:
                # 源给出了未登记 symbol：明确计数（不静默丢弃，也不猜测归属）
                self.stats.unknown_symbol_events += len(batch.events)
                continue
            if batch.source_error is not None:
                health.record_error(batch.source_error)
                continue
            for ev in batch.events:
                flat.append((int(ev.exchange_ts), arrival, batch.symbol, ev))
                arrival += 1
        flat.sort(key=lambda x: (x[0], x[2], x[1]))

        for ts, _arr, symbol, event in flat:
            self._process_one(symbol, event, ts)

    def _process_one(self, symbol: str, event: MarketEvent, ts: int) -> None:
        health = self.stats.health[symbol]
        # per-symbol 单调时间门：乱序（严格回退）跳过计数，不用未来/旧事件污染因果
        if health.last_exchange_ts is not None and ts < health.last_exchange_ts:
            health.out_of_order_skipped += 1
            health.last_error = f"out_of_order ts={ts} < {health.last_exchange_ts}"
            return
        key = (ts, event.event_type.value, _event_digest(event))
        if ts == health.last_exchange_ts and key == health.last_event_key:
            health.duplicate_skipped += 1
            return
        try:
            process_market_event(
                event=event, now=ts, engine=self._engine, features=self._features,
                decision_fn_for=self._fn_for, config=self._step_config,
                counters=self.stats.counters, prediction_runtime=self._prediction)
        except KeyError as exc:
            # 未知 symbol 等边界错误：隔离该事件并计数，不杀全局
            self.stats.unknown_symbol_events += 1
            health.record_error(f"KeyError: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 单标的处理失败隔离
            health.record_error(f"{type(exc).__name__}: {exc}")
            return
        health.healthy = True
        health.events_seen += 1
        health.last_exchange_ts = ts
        health.last_event_key = key

    # ------------------------------------------------------------------ 停止
    def _shutdown(self) -> None:
        self.stats.status = RunStatus.STOPPING
        now = max((h.last_exchange_ts or 0) for h in self.stats.health.values())
        # 1) 先 drain 一次在途成交
        tail = self._engine.poll(now_ms=max(now, 0))
        self.stats.counters.fills += len(tail.fills)
        # 2) 撤销所有 active 模拟挂单并验证终态（不强平、不虚构收盘价）
        for order in tuple(self._tracker.active()):
            self.stats.stop_cancel_requested += 1
            try:
                self._engine.cancel(order.client_order_id, now_ms=max(now, 0))
            except Exception as exc:  # noqa: BLE001
                h = self.stats.health.get(order.symbol)
                if h is not None:
                    h.last_error = f"cancel_failed: {type(exc).__name__}: {exc}"
        drained = self._engine.poll(now_ms=max(now, 0))
        self.stats.counters.fills += len(drained.fills)
        remaining = tuple(o.client_order_id for o in self._tracker.active())
        self.stats.open_after_stop = remaining
        self.stats.stop_cancel_confirmed = self.stats.stop_cancel_requested - len(remaining)
        try:
            self._source.close()
        except Exception:  # noqa: BLE001
            pass
        self.stats.status = RunStatus.STOPPED


def _event_digest(event: MarketEvent) -> str:
    """事件内容的轻量指纹（用于同 ts 重复检测；不持久化、不参与业务）。"""
    payload = event.payload
    parts = [type(payload).__name__]
    for attr in ("price", "qty", "quantity", "side", "trade_id", "last_update_id"):
        if hasattr(payload, attr):
            parts.append(f"{attr}={getattr(payload, attr)!r}")
    return "|".join(parts)


__all__ = [
    "LiveRunError",
    "RunStatus",
    "SymbolBatch",
    "BatchSource",
    "LiveRiskConfig",
    "SymbolHealth",
    "LiveRunStats",
    "LiveMultiSymbolRunner",
]
