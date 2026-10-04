"""Runtime decision loop（P0001.14）：Market → Prediction → MakerPolicy → Risk → Readiness → Execution。

边界纪律：

- **只做编排与生命周期**：所有业务语义仍由既有 Owner 拥有（`PredictionRuntime` / `MakerPolicy` / `RiskGate` /
  `ExecutionEngine` / `OrderTracker` / `AccountingCore`）；本模块**不**算价格 / 不判风险 / 不生成预测；
- **不依赖具体 venue/connector**：只面向 `ExecutionEngine`（adapter/connector seam 在 engine 内），
  不 import Binance/Paper 实现；
- 单线程 + 自有 asyncio event loop（`PredictionRuntime.submit` 是 async）；`stop()` 取消任务、关闭 loop、join 线程，
  不遗留 background thread；
- **节流**：只有 market state hash 变化 / prediction 过期 / decision cadence 到期，才重新预测或决策；
  不是每个 tick 都调用 provider（provider 不进入毫秒级热路径）。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from market.events.types import Milliseconds
from product.types import RuntimeMode
from strategy.maker.types import QuoteAction

#: PAPER 的 readiness 语义（人类裁决 2026-10-03 方案 A）：PAPER 不经 live readiness gate，但 causal chain 仍记录。
PAPER_READINESS_REASON = "PAPER_LIVE_READINESS_NOT_APPLICABLE"
LIVE_READINESS_NOT_EVALUATED = "LIVE_READINESS_NOT_EVALUATED"


class DecisionLoopError(RuntimeError):
    """决策 loop 契约错误。"""


@dataclass(frozen=True, slots=True)
class ReadinessFact:
    """loop 侧 readiness 事实（duck-typed；ProductService 只读取 status/reasons/scope/live_ready）。"""

    status: str
    reasons: tuple[str, ...] = ()
    scope: str | None = None
    detail: str = ""
    live_ready: bool = False
    #: live readiness 是否适用于当前 mode（PAPER/REPLAY ⇒ False，记录但不作为 submit authority）
    applicable: bool = True


@dataclass(frozen=True, slots=True)
class DecisionLoopConfig:
    """loop 配置（全部显式）。"""

    symbol: str
    mode: RuntimeMode
    tick_ms: int = 500
    decision_interval_ms: int = 5_000
    prediction_min_interval_ms: int = 1_000
    prediction_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise DecisionLoopError("DecisionLoopConfig.symbol must be a non-empty string")
        if not isinstance(self.mode, RuntimeMode):
            raise DecisionLoopError("DecisionLoopConfig.mode must be a RuntimeMode")
        for name in ("tick_ms", "decision_interval_ms", "prediction_min_interval_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise DecisionLoopError(f"DecisionLoopConfig.{name} must be a positive int")
        if not isinstance(self.prediction_enabled, bool):
            raise DecisionLoopError("DecisionLoopConfig.prediction_enabled must be a bool")

    @property
    def write_enabled(self) -> bool:
        """只有 PAPER 允许真实提交（TESTNET/LIVE 的写权限不在本阶段范围）。"""
        return self.mode is RuntimeMode.PAPER

    @property
    def readiness_required(self) -> bool:
        """TESTNET/LIVE 必须经 live readiness authority；PAPER/REPLAY 不需要（方案 A）。"""
        return self.mode in (RuntimeMode.TESTNET, RuntimeMode.LIVE)


@dataclass
class DecisionLoopStatus:
    """loop 可观测事实（只读投影用）。"""

    running: bool = False
    ticks: int = 0
    predictions_submitted: int = 0
    predictions_accepted: int = 0
    decisions: int = 0
    submits: int = 0
    cancels: int = 0
    risk_rejects: int = 0
    readiness_blocks: int = 0
    errors: int = 0
    last_error: str | None = None
    last_prediction_at: Milliseconds | None = None
    last_decision_at: Milliseconds | None = None
    last_state_hash: str | None = None


@dataclass
class RuntimeDecisionLoop:
    """把既有 Owner 串成一条真实决策闭环（编排层）。"""

    config: DecisionLoopConfig
    state_provider: Callable[[], object | None]
    engine: object
    tracker: object
    risk_budget_provider: Callable[[object], float | None]
    clock: Callable[[], Milliseconds]
    policy: object | None = None
    prediction_runtime: object | None = None
    readiness_provider: Callable[[], object | None] | None = None
    kill_switch_provider: Callable[[], object] | None = None
    #: 快照前钩子（composition root 用它在同一条线程上把正式 MARK 推给 accounting owner）
    pre_snapshot: Callable[[], None] | None = None
    on_prediction: Callable[[object | None], None] | None = None
    on_decision: Callable[[object | None], None] | None = None
    on_readiness: Callable[[object | None], None] | None = None
    status: DecisionLoopStatus = field(default_factory=DecisionLoopStatus)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _pending_replace: dict[str, object] = field(default_factory=dict, init=False)
    _prediction: object | None = field(default=None, init=False)
    _decision: object | None = field(default=None, init=False)
    _readiness: object | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not callable(self.state_provider):
            raise DecisionLoopError("RuntimeDecisionLoop.state_provider must be callable")
        if not callable(self.risk_budget_provider):
            raise DecisionLoopError("RuntimeDecisionLoop.risk_budget_provider must be callable")
        if not callable(self.clock):
            raise DecisionLoopError("RuntimeDecisionLoop.clock must be callable")
        if self.policy is not None and not hasattr(self.policy, "decide"):
            raise DecisionLoopError("RuntimeDecisionLoop.policy must expose decide(...)")

    # ------------------------------------------------------------------ 只读事实

    @property
    def latest_prediction(self) -> object | None:
        return self._prediction

    @property
    def latest_decision(self) -> object | None:
        return self._decision

    @property
    def readiness_result(self) -> object | None:
        return self._readiness

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None:
            raise DecisionLoopError("decision loop already started")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"probex-decision-{self.config.symbol}",
                                        daemon=True)
        self._thread.start()
        self.status.running = True

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """graceful stop：置停止位 → join 线程（loop 随之关闭）；不遗留线程。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)
        self.status.running = bool(thread is not None and thread.is_alive())
        self._thread = None

    def tick_once(self) -> None:
        """同步执行一轮（测试/一次性驱动用；与线程内 `_tick` 同一实现）。"""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._tick())
        finally:
            loop.close()

    # ------------------------------------------------------------------ 内部

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:  # noqa: BLE001 - loop 崩溃必须可见
            self.status.errors += 1
            self.status.last_error = f"{type(exc).__name__}"
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    async def _main(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 - 单轮失败不得杀死 loop
                self.status.errors += 1
                self.status.last_error = f"{type(exc).__name__}"
            await asyncio.sleep(self.config.tick_ms / 1000.0)

    async def _tick(self) -> None:
        self.status.ticks += 1
        now = int(self.clock())
        state = self.state_provider()
        if state is None:
            return
        tradeable = bool(getattr(getattr(state, "quality", None), "tradeable", False))
        await self._maybe_predict(state, tradeable=tradeable, now=now)
        self._publish_readiness(self._readiness_fact())
        if tradeable and self._decision_due(state, now):
            self._decide_and_execute(state, now)
        self.engine.poll(now_ms=now)

    # ---------------------------------------------------------------- prediction

    async def _maybe_predict(self, state: object, *, tradeable: bool, now: Milliseconds) -> None:
        runtime = self.prediction_runtime
        if runtime is None:
            self._publish_prediction(None)
            return
        if not self.config.prediction_enabled or not tradeable:
            self._publish_prediction(self._latest_from(runtime))
            return
        latest = self._latest_from(runtime)
        state_hash = _state_hash(state)
        due = (latest is None or bool(runtime.is_expired(latest))
               or str(getattr(latest, "market_state_hash", "")) != state_hash)
        if not due:
            self._publish_prediction(latest)
            return
        last = self.status.last_prediction_at or 0
        if now - last < self.config.prediction_min_interval_ms:
            return
        retry_at = getattr(runtime, "next_retry_at", None)
        if retry_at is not None and now < int(retry_at):
            return
        self.status.predictions_submitted += 1
        self.status.last_prediction_at = now
        result = await runtime.submit(state)
        if getattr(result, "record", None) is not None:
            self.status.predictions_accepted += 1
        self._publish_prediction(self._latest_from(runtime))

    @staticmethod
    def _latest_from(runtime: object) -> object | None:
        return getattr(runtime, "latest_prediction", None)

    # ---------------------------------------------------------------- decision

    def _decision_due(self, state: object, now: Milliseconds) -> bool:
        if self.status.last_decision_at is None:
            return True
        if _state_hash(state) != self.status.last_state_hash:
            return True
        return (now - int(self.status.last_decision_at)) >= self.config.decision_interval_ms

    def _decide_and_execute(self, state: object, now: Milliseconds) -> None:
        # P0001.15 §12：在同一线程、紧邻快照之前注入正式 reference price（未知则什么也不做）
        if self.pre_snapshot is not None:
            self.pre_snapshot()
        snapshot = self.engine.snapshot(self.config.symbol, now_ms=now)   # type: ignore[attr-defined]
        if self.policy is None:
            # 无 MakerPolicy（配置缺失）⇒ 不产生 decision（诚实 ABSENT；不伪造 HOLD/BUY）
            self._publish_decision(None)
            self._mark_decision(state, now)
            return
        budget = self.risk_budget_provider(snapshot)
        position = self.engine.accounting.position(self.config.symbol)     # type: ignore[attr-defined]
        existing = tuple(self.tracker.active())                            # type: ignore[attr-defined]
        decision = self.policy.decide(                                     # type: ignore[attr-defined]
            state=state, prediction=self._prediction, position=position, snapshot=snapshot,
            existing_orders=existing,
            remaining_risk_budget=(None if budget is None else float(budget)),
            kill_switch=self._kill_switch())
        self.status.decisions += 1
        self._publish_decision(decision)
        if self.config.write_enabled:
            self._apply(decision, now)
        self._mark_decision(state, now)

    def _apply(self, decision: object, now: Milliseconds) -> None:
        allowed, readiness = self._readiness_verdict()
        self._publish_readiness(readiness)
        for side in (getattr(decision, "bid", None), getattr(decision, "ask", None)):
            if side is None:
                continue
            action = getattr(side, "action", None)
            if action in (QuoteAction.KEEP, QuoteAction.NONE, None):
                continue
            client_order_id = getattr(side, "client_order_id", None)
            if action is QuoteAction.CANCEL:
                self._cancel(client_order_id, now)
            elif action is QuoteAction.REPLACE:
                if self._cancel(client_order_id, now):
                    self._pending_replace[str(client_order_id)] = side      # cancel-before-replace
            elif action is QuoteAction.PLACE:
                self._submit(side, now, allowed=allowed)
        self._drain_pending_replace(now, allowed=allowed)

    def _drain_pending_replace(self, now: Milliseconds, *, allowed: bool) -> None:
        for client_order_id, side in list(self._pending_replace.items()):
            order = self.tracker.order(client_order_id)                    # type: ignore[attr-defined]
            if order is None or not getattr(order, "is_terminal", False):
                continue
            self._pending_replace.pop(client_order_id, None)
            self._submit(side, now, allowed=allowed)

    def _submit(self, side: object, now: Milliseconds, *, allowed: bool) -> None:
        if self.config.readiness_required and not allowed:
            self.status.readiness_blocks += 1
            return
        proposal = getattr(side, "proposal", None)
        if proposal is None:
            return
        result = self.engine.submit(proposal, now_ms=now)                   # type: ignore[attr-defined]
        if getattr(result, "submitted", False):
            self.status.submits += 1
        elif getattr(result, "rejected", False):
            self.status.risk_rejects += 1

    def _cancel(self, client_order_id: object, now: Milliseconds) -> bool:
        if not client_order_id:
            return False
        self.engine.cancel(str(client_order_id), now_ms=now)                # type: ignore[attr-defined]
        self.status.cancels += 1
        return True

    # ---------------------------------------------------------------- readiness

    def _kill_switch(self) -> object:
        """kill switch 由 Risk limits 拥有（本层只读取，不推断）。"""
        from risk.types import KillSwitchMode

        if self.kill_switch_provider is None:
            return KillSwitchMode.NORMAL
        try:
            return self.kill_switch_provider()
        except Exception:  # noqa: BLE001 - 读不到就 fail closed？不：由 provider 决定；此处回默认
            return KillSwitchMode.NORMAL

    def _readiness_fact(self) -> object:
        provided = self.readiness_provider() if self.readiness_provider is not None else None
        if provided is not None:
            return provided
        if self.config.readiness_required:
            return ReadinessFact(status="unavailable", reasons=(LIVE_READINESS_NOT_EVALUATED,),
                                 detail="live readiness authority not evaluated in this runtime")
        # PAPER / REPLAY（人类裁决方案 A）：记录但不作为 live gate
        return ReadinessFact(status="unavailable", reasons=(PAPER_READINESS_REASON,),
                             detail="PAPER/REPLAY does not use the live readiness gate as submit authority",
                             applicable=False)

    def _readiness_verdict(self) -> tuple[bool, object]:
        fact = self._readiness_fact()
        if not self.config.readiness_required:
            return True, fact
        return bool(getattr(fact, "live_ready", False)), fact

    # ---------------------------------------------------------------- publish

    def _publish_prediction(self, record: object | None) -> None:
        self._prediction = record
        if self.on_prediction is not None:
            self.on_prediction(record)

    def _publish_decision(self, decision: object | None) -> None:
        self._decision = decision
        if self.on_decision is not None:
            self.on_decision(decision)

    def _publish_readiness(self, fact: object | None) -> None:
        self._readiness = fact
        if self.on_readiness is not None:
            self.on_readiness(fact)

    def _mark_decision(self, state: object, now: Milliseconds) -> None:
        self.status.last_decision_at = now
        self.status.last_state_hash = _state_hash(state)


def _state_hash(state: object) -> str:
    """canonical market state identity（复用既有 prediction schema）。"""
    from prediction.schema import market_state_hash

    return market_state_hash(state)


__all__ = [
    "LIVE_READINESS_NOT_EVALUATED", "PAPER_READINESS_REASON", "DecisionLoopConfig",
    "DecisionLoopError", "DecisionLoopStatus", "ReadinessFact", "RuntimeDecisionLoop",
]
