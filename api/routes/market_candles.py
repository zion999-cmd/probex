"""Candles（chart workbench）：由 server 从 `ProductService` 的有界市场缓冲聚合生成。"""

from __future__ import annotations

PATH = "/api/v1/market/candles"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点由 server 直接生成
    raise KeyError("market_candles is aggregated from the bounded market history")
