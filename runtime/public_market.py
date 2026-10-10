"""产品侧公开行情泵（统一数据源扩展点）。

职责
----
在单线程上驱动**任何** `MarketSourceAdapter`（免费公开行情或未来付费源），把其产出的
`MarketState` 搬进既有 `BoundedMarketHistory`，并把正式 mark 通知既有 accounting Owner
（仅当显式配置）。只读：不构造 private / execution / broker ⇒ 不能下单。

设计纪律
--------
- 数据源经适配器转换为**既有 `MarketState` 契约**；不为某个源另建状态机或存储。
- 落盘 run 级事实仍走既有 `RunRegistry`（由 assembly 的 sink 完成）。
- 连接/传输错误由适配器处理；重连耗尽或 `poll_states` 抛错时 pump 记录并停止，
  产品面 connector health 变为 FAILED/RECONNECTING ⇒ 不假报健康、不伪造数据。
- 生命周期：单线程、daemon；`stop()` 设置停止标志并 disconnect 适配器。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from market.events.types import Milliseconds
from venue.market_source import MarketSourceAdapter
from venue.health import MarketConnectorHealth


class PublicMarketPump:
    """在单线程上把一个公开行情源适配器的事实泵入既有展示缓冲（只读）。"""

    def __init__(
        self,
        adapter: MarketSourceAdapter,
        *,
        history: object,
        clock: Callable[[], Milliseconds],
        connector: object | None = None,
        pump_timeout_s: float = 0.5,
        max_states: int = 256,
        on_mark: Callable[[float, int], None] | None = None,
        on_state: Callable[[object], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._history = history
        self._clock = clock
        self._connector = connector
        self._pump_timeout_s = pump_timeout_s
        self._max_states = max_states
        self._on_mark = on_mark
        self._on_state = on_state
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_at: Milliseconds = 0
        self._states = 0
        self._last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def states_seen(self) -> int:
        return self._states

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def adapter(self) -> MarketSourceAdapter:
        return self._adapter

    def attach_connector(self, connector: object) -> None:
        """可选：注入 `MarketDataConnector`（assembly 用于 health 投影）。"""
        self._connector = connector

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._started_at = int(self._clock())
        self._thread = threading.Thread(
            target=self._run, name=f"probex-market:{self._adapter.source_id}", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        try:
            self._adapter.disconnect()
        except Exception:  # noqa: BLE001 - 关闭失败不得掩盖 stop
            self._last_error = self._last_error or "disconnect_failed"

    # ------------------------------------------------------------------ 线程主体

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                states = self._adapter.poll_states(
                    timeout_s=self._pump_timeout_s, max_states=self._max_states)
            except Exception as exc:  # noqa: BLE001 - 记录后停止（不假报健康；适配器已尝试其重连）
                self._last_error = type(exc).__name__
                break
            for state in states:
                self._states += 1
                self._history.feed_state(state)
                if self._on_state is not None:
                    try:
                        self._on_state(state)
                    except Exception:  # noqa: BLE001 - sink 失败不打断 pump
                        pass
            if states and self._on_mark is not None:
                mark = self._adapter.latest_mark()
                if mark is not None:
                    try:
                        self._on_mark(float(mark.price), int(getattr(mark, "receive_ts",
                                                                     getattr(mark, "exchange_ts"))))
                    except Exception:  # noqa: BLE001
                        pass


__all__ = ["PublicMarketPump"]
