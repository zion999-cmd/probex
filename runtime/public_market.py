"""产品侧真实公网行情泵（P0001.17 → 全局补齐 Batch 3：G-A2 / G-B2）。

目的
----
让产品入口（`runtime.assembly`）可以选择**持续真实公网行情**作为馈送源，而不是只能吃
一个（可能是合成的）event store 文件。数据来自既有 `LiveMarketDataRuntime`
（Binance USDⓈ-M 公开 REST + WS；P0001.9.1 已验证；**无需任何凭据**）。

设计纪律
--------
- **只读**：只构造 public runtime，不构造任何 private / execution / broker ⇒ 不能下单。
- **复用既有 Owner**：真实事实的唯一来源仍是 `LiveMarketDataRuntime`（其 FeatureEngine 拥有
  MarketState）；本模块只在一条 pump 线程上调用 `pump_once`，把产出搬进**既有**
  `BoundedMarketHistory`（只读展示缓冲），并在有正式 mark 时调用**既有** accounting Owner
  （仅当产品显式配置了 accounting）。
- **不新建存储**：落盘 run 级事实仍走既有 `RunRegistry`（由 assembly 的 sink 完成）。
- 生命周期：单线程、daemon、`stop()` 设置停止标志并 `close()` runtime（与 decision loop 同一风格）。

故障语义
--------
连接 / 传输错误按既有 `LiveMarketDataRuntime` 的重连策略处理；重连耗尽时 pump 线程把错误记录
到 telemetry 并停止（产品面 connector health 变为 RECONNECTING/FAILED ⇒ 不假报健康）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from market.events.payloads import BookSnapshotPayload, TradePayload
from market.events.types import Milliseconds


@dataclass(frozen=True, slots=True)
class PublicMarketConfig:
    """公网行情泵配置（全部显式；无业务数值默认）。"""

    symbol: str
    rest_base: str
    ws_host: str
    pump_timeout_s: float = 0.5
    max_messages: int = 256
    history_capacity: int = 6_000

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("PublicMarketConfig.symbol must be non-empty")
        if self.pump_timeout_s <= 0:
            raise ValueError("PublicMarketConfig.pump_timeout_s must be positive")
        if self.max_messages < 1:
            raise ValueError("PublicMarketConfig.max_messages must be >= 1")
        if self.history_capacity < 1:
            raise ValueError("PublicMarketConfig.history_capacity must be >= 1")


class PublicMarketPump:
    """在单线程上把真实公网行情泵入既有展示缓冲（只读）。"""

    def __init__(
        self,
        config: PublicMarketConfig,
        *,
        history: object,
        clock: Callable[[], Milliseconds],
        on_mark: Callable[[float, int], None] | None = None,
        on_event: Callable[[object], None] | None = None,
    ) -> None:
        self._config = config
        self._history = history
        self._clock = clock
        self._on_mark = on_mark
        self._on_event = on_event
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._runtime: object | None = None
        self._connector: object | None = None
        self._started_at: Milliseconds = 0
        self._messages = 0
        self._last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def messages(self) -> int:
        return self._messages

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def attach_runtime(self, runtime: object, connector: object) -> None:
        """注入由 assembly 构造的既有 public runtime + connector（便于 health 投影）。"""
        self._runtime = runtime
        self._connector = connector

    def start(self) -> None:
        if self.is_running:
            return
        if self._runtime is None:
            raise RuntimeError("attach_runtime(...) must be called before start()")
        self._stop.clear()
        self._started_at = int(self._clock())
        self._thread = threading.Thread(
            target=self._run, name="probex-public-market", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        runtime = self._runtime
        if runtime is not None:
            try:
                runtime.close()
            except Exception:  # noqa: BLE001 - 关闭失败不得掩盖 stop
                self._last_error = type(Exception()).__name__ or None

    # ------------------------------------------------------------------ 线程主体

    def _run(self) -> None:
        runtime = self._runtime
        assert runtime is not None  # noqa: S101
        while not self._stop.is_set():
            try:
                batch = runtime.pump_once(
                    timeout_s=self._config.pump_timeout_s,
                    max_messages=self._config.max_messages)
            except Exception as exc:  # noqa: BLE001 - 记录后停止（不假报健康；既有重连已在 runtime 内尝试）
                self._last_error = f"{type(exc).__name__}: ..."
                break
            self._consume(batch)
        self._last_error = self._last_error or None

    def _consume(self, batch: object) -> None:
        """把一批真实事实搬进既有展示缓冲 + 通知 sink（字段搬运；不推测）。"""
        for state in getattr(batch, "states", ()) or ():
            self._history.feed_state(state)
        for event in getattr(batch, "market_events", ()) or ():
            self._messages += 1
            payload = getattr(event, "payload", None)
            if isinstance(payload, BookSnapshotPayload):
                self._history.feed_snapshot(SimpleNamespace(
                    ts=int(event.exchange_ts),
                    bids=tuple((level.price, level.size) for level in payload.bids),
                    asks=tuple((level.price, level.size) for level in payload.asks)))
            elif isinstance(payload, TradePayload):
                self._history.feed_trade(SimpleNamespace(
                    ts=int(event.exchange_ts), price=float(payload.price),
                    quantity=float(payload.quantity),
                    aggressor=str(getattr(payload.aggressor, "value", payload.aggressor))))
            if self._on_event is not None:
                try:
                    self._on_event(event)
                except Exception:  # noqa: BLE001 - sink 失败不打断 pump
                    pass
        mark = getattr(batch, "mark", None)
        if mark is not None and self._on_mark is not None:
            try:
                self._on_mark(float(mark.price), int(mark.receive_ts))
            except Exception:  # noqa: BLE001
                pass


__all__ = ["PublicMarketConfig", "PublicMarketPump"]
