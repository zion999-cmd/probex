"""`GET /api/v1/capabilities`：能力清单（由 server 从路由表 + CLI 命令表生成）。"""

from __future__ import annotations

PATH = "/api/v1/capabilities"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点由 server 直接生成
    raise KeyError("capabilities is generated from the route/CLI registries")
