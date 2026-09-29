"""`GET /api/v1/portfolio/timeline`：equity / exposure 时间线（P0001.12.2 / G3，只读、有界）。"""

from __future__ import annotations

PATH = "/api/v1/portfolio/timeline"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 由 server 直接处理
    raise KeyError("account timeline is served from the bounded account buffer")
