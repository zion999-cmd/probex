"""`GET /api/v1/reports/run-summary`：只读 Run Summary（P0001.10.2 §4）。

该端点不参与 snapshot 的组合（报告需要 telemetry 计数等事实），由 server 直接向
`ProductService.run_summary_view()` 取事实；缺失 ⇒ 503（未知 ≠ 空报告）。
"""

from __future__ import annotations

PATH = "/api/v1/reports/run-summary"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点绕过 snapshot 组合
    raise KeyError("run-summary is served from ProductService.run_summary_view()")
