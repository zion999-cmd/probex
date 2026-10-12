"""P0017.5：把 P0017.1 的 `MultiSymbolMarketCollector` 适配为实时模拟交易的批次源。

这是**唯一**接触实时采集器的接线点：通过 collector 的 `on_batch(symbol, MarketDataBatch)`
回调拿到每轮真实 `MarketDataBatch.market_events`（与离线回测消费的同一 `MarketEvent` 类型），
交给 `LiveMultiSymbolRunner`。

- 采集器本身只读、无下单能力；runner 的执行适配器固定为 `MultiSymbolSimulatedAdapter`。
- 单标的采集错误（collector 在 `pump_once()` 摘要里以 {"error": ...} 报告）转为
  `SymbolBatch(source_error=...)`，由 runner 做故障隔离，不终止健康标的。
- 本模块不 import `runtime.testnet` 或任何 binance 写执行 connector（AST 守卫覆盖）。

runner 在单线程内**同步**调用 collector.pump_once()（不启动 collector 后台线程），
保证"事件→决策→提交"的单线程确定性；collector 支持离线测试直接 pump。
"""

from __future__ import annotations

from connectors.binance.market_data.multi_collector import MultiSymbolMarketCollector
from market.events.types import MarketEvent
from runtime.live_multi_symbol import SymbolBatch


class CollectorBatchSource:
    """同步驱动 `MultiSymbolMarketCollector.pump_once()`，并经 on_batch 取出真实事件。

    可传入已构造的 collector（会包一层 on_batch 汇聚其每轮批次）；该 collector 不应再启动
    自己的后台线程（由本 runner 单线程 pump）。
    """

    def __init__(self, collector: MultiSymbolMarketCollector) -> None:
        self._collector = collector
        self._symbols: tuple[str, ...] = tuple(collector.symbols)
        self._pending: dict[str, list[MarketEvent]] = {s: [] for s in self._symbols}
        self._errors: dict[str, str] = {}

        original_on_batch = collector._on_batch  # type: ignore[attr-defined]

        def _capture(symbol: str, batch: object) -> None:
            if original_on_batch is not None:
                try:
                    original_on_batch(symbol, batch)
                except Exception:  # noqa: BLE001 - 不影响事件捕获
                    pass
            for ev in getattr(batch, "market_events", ()):  # type: ignore[union-attr]
                self._pending.setdefault(symbol, []).append(ev)

        collector._on_batch = _capture  # type: ignore[attr-defined]
        if not getattr(collector, "_connected", False):
            collector.connect()

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def pump(self) -> tuple[SymbolBatch, ...]:
        summary = self._collector.pump_once()
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
            self._collector.stop()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["CollectorBatchSource"]
