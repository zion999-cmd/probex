"""Assistant 端点（P0001.12.3 §1）：只读上下文 + 建议动作（建议全部来自 Action Manifest）。"""

from __future__ import annotations

PATH = "/api/v1/assistant/context"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 由 server 直接处理
    raise KeyError("assistant context is served from the Assistant service")
