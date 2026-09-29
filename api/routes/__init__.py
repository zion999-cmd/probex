"""产品 API 路由表（只读；P0001.10 §控制面边界）。"""

from __future__ import annotations

from api.routes import (
    evidence,
    execution,
    market,
    portfolio,
    prediction,
    readiness,
    reports,
    risk,
    status,
    strategy,
)

MODULES = (status, market, prediction, strategy, risk, execution, portfolio, readiness, evidence)
#: 需要 service 侧 telemetry 事实的端点（不参与 snapshot 组合）
REPORT_PATH = reports.PATH

#: path -> 模块（第一版全部为 GET，只读）
ROUTES = {module.PATH: module for module in MODULES}
#: 兼容别名（同一只读切片）
for _module in MODULES:
    for _alias in getattr(_module, "ALIASES", ()):
        ROUTES[_alias] = _module

__all__ = ["MODULES", "ROUTES"]
