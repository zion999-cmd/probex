"""执行安全只读端点（P0001.13）：health / limits / rate-limits / latency / anomalies / reconciliation。"""

from __future__ import annotations

PATH = "/api/v1/execution"
HEALTH_PATH = "/api/v1/execution/health"
LIMITS_PATH = "/api/v1/execution/limits"
RATE_LIMITS_PATH = "/api/v1/execution/rate-limits"
LATENCY_PATH = "/api/v1/execution/latency"
ANOMALIES_PATH = "/api/v1/execution/anomalies"
RECONCILIATION_PATH = "/api/v1/execution/reconciliation"
SECTIONS = ()

SUB_PATHS = {HEALTH_PATH: "health", LIMITS_PATH: "limits", RATE_LIMITS_PATH: "rate-limits",
             LATENCY_PATH: "latency", ANOMALIES_PATH: "anomalies",
             RECONCILIATION_PATH: "reconciliation"}


def payload(snapshot: dict) -> dict:  # pragma: no cover - 由 server 直接处理
    raise KeyError("execution safety facts are served from the execution safety projection")
