"""L2 depth 热图（P0001.12 §2）：由 server 从 `ProductService` 的有界展示缓冲投影生成。"""

from __future__ import annotations

PATH = "/api/v1/market/depth"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点由 server 直接生成
    raise KeyError("market_depth is projected from the bounded market history")
