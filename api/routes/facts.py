"""`GET /api/v1/facts/<kind>/<identity>`：Raw Facts drill-down（P0001.12.2 / G2，只读、有界）。"""

from __future__ import annotations

PATH = "/api/v1/facts"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 由 server 直接处理
    raise KeyError("raw facts are served from the product fact lookup")
