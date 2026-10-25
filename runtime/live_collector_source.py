"""P0017.5：把 P0017.1 的 `MultiSymbolMarketCollector` 适配为实时模拟交易的批次源。

这是**唯一**接触实时采集器的接线点：通过 collector 的公开批次契约
（`set_batch_sink` / `connected` / `connect` / `pump_once` / `stop`）
拿到每轮真实 `MarketDataBatch.market_events`（与离线回测消费的同一 `MarketEvent` 类型），
交给 `LiveMultiSymbolRunner`。不依赖 collector 任何私有字段。

- 采集器本身只读、无下单能力；runner 的执行适配器固定为 `MultiSymbolSimulatedAdapter`。
- 单标的采集错误（collector 在 `pump_once()` 摘要里以 {"error": ...} 报告）转为
  `SymbolBatch(source_error=...)`，由 runner 做故障隔离，不终止健康标的。
- sink 异常由 collector 自己在 `pump_once()` 内隔离；若构造前已有 sink，
  经 `set_batch_sink` 返回值保持链式调用。
- 本模块不 import `runtime.testnet` 或任何 binance 写执行 connector（AST 守卫覆盖）。

runner 在单线程内**同步**调用 collector.pump_once()（不启动 collector 后台线程），
保证"事件→决策→提交"的单线程确定性；collector 支持离线测试直接 pump。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from market.events.types import MarketEvent
from runtime.live_multi_symbol import SymbolBatch

if TYPE_CHECKING:
    # 只读公开行情采集器（不引入写/testnet 路径；隔离测试固定此 import root）
    from connectors.binance.market_data.multi_collector import MultiSymbolMarketCollector

#: collector 需要满足的公开契约（duck typing；真实实现 = MultiSymbolMarketCollector）
BatchSink = Callable[[str, object], None]


class CollectorBatchSource:
    """同步驱动 collector.pump_once()，并经公开 sink 契约取出真实事件。

    可传入已构造的 collector：本对象经 `set_batch_sink()` 安装捕获 sink；
    若 collector 尚未连接则调用公开 `connect()`。该 collector 不应再启动
    自己的后台线程（由本 runner 单线程 pump）。
    """

    def __init__(self, collector: MultiSymbolMarketCollector) -> None:
        self._collector = collector
        self._symbols: tuple[str, ...] = tuple(collector.symbols)  # type: ignore[attr-defined]
        self._pending: dict[str, list[MarketEvent]] = {s: [] for s in self._symbols}
        self._errors: dict[str, str] = {}

        previous_sink = collector.set_batch_sink(self._capture)  # type: ignore[attr-defined]
        self._previous_sink: BatchSink | None = previous_sink
        if not collector.connected:  # type: ignore[attr-defined]
            collector.connect()  # type: ignore[attr-defined]

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def _capture(self, symbol: str, batch: object) -> None:
        if self._previous_sink is not None:
            self._previous_sink(symbol, batch)
        for event in getattr(batch, "market_events", ()):
            self._pending.setdefault(symbol, []).append(event)

    def pump(self) -> tuple[SymbolBatch, ...]:
        summary = self._collector.pump_once()  # type: ignore[attr-defined]
        batches: list[SymbolBatch] = []
        for symbol in self._symbols:
            result = summary.get(symbol, {})
            if isinstance(result, dict) and "error" in result:
                self._errors[symbol] = str(result["error"])
                batches.append(SymbolBatch(symbol=symbol, events=(),
                                             source_error=str(result["error"])))
                continue
            events = tuple(self._pending.get(symbol, ()))
            self._pending[symbol] = []
            err = self._errors.pop(symbol, None) if not events else None
            batches.append(SymbolBatch(symbol=symbol, events=events, source_error=err))
        return tuple(batches)

    def close(self) -> None:
        try:
            self._collector.stop()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 关闭路径 best-effort
            pass


__all__ = ["CollectorBatchSource"]
