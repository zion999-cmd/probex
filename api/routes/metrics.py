"""`GET /api/v1/metrics`：Metric Contract 定义（P0001.11 §3；UI 只能读定义，不能自己算）。"""

from __future__ import annotations

PATH = "/api/v1/metrics"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点由 server 直接生成
    raise KeyError("metrics definitions are served from reports.metrics")
