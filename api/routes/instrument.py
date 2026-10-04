"""`GET /api/v1/instrument`：instrument / venue / reference price / connector health（P0001.15 §21–§26）。

只组合事实：本模块不做任何计算，只从 snapshot 中挑选对应段落（两个 connector health **分开**）。
"""

from __future__ import annotations

PATH = "/api/v1/instrument"
ALIASES = ("/api/v1/venue",)
SECTIONS = ("runtime", "instrument", "venue", "reference_price",
            "market_connector_health", "private_connector_health")


def payload(snapshot: dict) -> dict:
    """从序列化后的 snapshot 中取本路由需要的段落（缺失 ⇒ 契约错误）。"""
    missing = [section for section in SECTIONS if section not in snapshot]
    if missing:
        raise KeyError(f"snapshot is missing sections: {missing}")
    return {section: snapshot[section] for section in SECTIONS}
