"""P0017.1：发现到的可交易对（universe）。由 server 从 market platform 投影生成。"""

from __future__ import annotations

PATH = "/api/v1/market/universe"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - server 直接生成
    raise KeyError("market universe is provided by the market platform projection")
