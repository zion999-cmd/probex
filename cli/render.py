"""CLI 文本渲染（人类可读）：确定性、UNKNOWN 显式。数据走 stdout，诊断走 stderr。"""

from __future__ import annotations

from product.types import Fact


def fact_text(fact: Fact | dict) -> str:
    """接受 `Fact` 或**已序列化**的 `{"known", "value", "reason"}` 两种形态。"""
    if isinstance(fact, dict):
        if not fact.get("known"):
            return f"UNKNOWN ({fact.get('reason')})"
        return str(fact.get("value"))
    if not fact.known:
        return f"UNKNOWN ({fact.reason})"
    return str(fact.value)


def rows(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return "(none)"
    width = max(len(key) for key, _ in pairs)
    return "\n".join(f"{key.ljust(width)}  {value}" for key, value in pairs)


def fact_rows(payload: dict) -> str:
    return rows([(key, fact_text(value)) for key, value in payload.items()
                 if isinstance(value, dict) and "known" in value])


def list_text(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) if values else "(none)"
