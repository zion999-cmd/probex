"""策略/执行 overlay（P0001.12 §8/§9）：由 server 从有界历史投影生成。"""

from __future__ import annotations

PATH = "/api/v1/market/overlays"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点由 server 直接生成
    raise KeyError("overlays are projected from the bounded market history")
