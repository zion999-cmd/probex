"""多标的市场数据平台的只读投影（P0017.1）。

把采集编排层（collector）+ 共享数据集（MarketDatasetReader）暴露为 Product API 可直接
序列化的事实。**产品层纪律**：不 import 任何外部行情适配器（发现/采集在 runtime 层完成，
本模块只接收已解析的 callable 与只读数据集路径），不拥有市场真值、不采集、不写盘。

- universe：发现到的可交易对（provider 返回已解析的 dict-like 视图）。
- subscriptions：已订阅标的的采集健康/水位（每标的独立）。
- history：从与 run 解耦的共享数据集读取（无数据 ⇒ 显式空，不伪造）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# 产品层不依赖外部适配器或存储实现；reader 经工厂注入（由组合根提供）
ReaderFactory = Callable[..., object]


@dataclass(frozen=True, slots=True)
class MarketPlatform:
    """市场数据平台只读事实源（组合根构造；未接线 ⇒ 端点 503）。"""

    venue: str = "binance"
    #: 返回 universe 视图（dict，含 symbols/quote_assets/...）；未接线 ⇒ None
    universe_provider: Callable[[], dict[str, object] | None] | None = None
    #: 返回多标的 collector（只读订阅状态）；未接线 ⇒ None
    collector_provider: Callable[[], object | None] | None = None
    dataset_root: str | Path | None = None
    #: 数据集 reader 工厂（组合根注入，避免本模块 import storage）
    reader_factory: ReaderFactory | None = None

    def universe_view(self) -> dict[str, object] | None:
        provider = self.universe_provider
        if provider is None:
            return None
        view = provider()
        if view is None:
            return None
        view["venue"] = self.venue
        return view

    def subscriptions_view(self) -> dict[str, object] | None:
        provider = self.collector_provider
        if provider is None:
            return None
        collector = provider()
        if collector is None:
            return None
        subs = collector.subscriptions()
        return {
            "venue": self.venue,
            "subscribed_count": len(subs),
            "symbols": [item.view() for item in subs],
        }

    def symbol_status_view(self, symbol: str) -> dict[str, object] | None:
        provider = self.collector_provider
        if provider is None:
            return None
        collector = provider()
        if collector is None or symbol not in collector.symbols:
            return None
        for item in collector.subscriptions():
            if item.symbol == symbol:
                return {"venue": self.venue, **item.view()}
        return None

    def dataset_view(self, symbol: str, kind: str, *, start_ts: int | None,
                      end_ts: int | None, limit: int) -> dict[str, object] | None:
        if self.dataset_root is None or self.reader_factory is None:
            return None
        reader = self.reader_factory(self.dataset_root, venue=self.venue, symbol=symbol, kind=kind)
        if not reader.exists:
            return {"venue": self.venue, "symbol": symbol, "kind": kind,
                    "source": "market-dataset", "records": [], "partitions": [], "gaps": [],
                    "note": "no recorded data for this symbol/kind yet"}
        records = reader.read(start_ts=start_ts, end_ts=end_ts, limit=limit)
        return {
            "venue": self.venue,
            "symbol": symbol,
            "kind": kind,
            "source": "market-dataset",
            "records": [
                {"exchange_ts": r.exchange_ts, "receive_ts": r.receive_ts, "source": r.source,
                 "payload": r.payload, "quality": r.quality}
                for r in records
            ],
            "partitions": [
                {"day": p.day, "rows": p.rows, "first_ts": p.first_ts, "last_ts": p.last_ts,
                 "bytes": p.bytes_, "sha256": p.sha256}
                for p in reader.partitions()
            ],
            "gaps": [{"day": d, "reason": why} for d, why in reader.gaps()],
        }


__all__ = ["MarketPlatform"]
