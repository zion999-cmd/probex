"""时间线投影（P0001.12 §1）：由 server 从 `ProductService` 的有界展示缓冲投影生成。"""

from __future__ import annotations

PATH = "/api/v1/market/timeline"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点由 server 直接生成
    raise KeyError("market_timeline is projected from the bounded market history")
