"""产品 API 路由表（只读；P0001.10 §控制面边界）。"""

from __future__ import annotations

from api.routes import (
    blockers,
    capabilities,
    evidence,
    execution,
    market,
    metrics,
    portfolio,
    prediction,
    readiness,
    reports,
    runs,
    risk,
    status,
    strategy,
)

MODULES = (status, market, prediction, strategy, risk, execution, portfolio, readiness, evidence, blockers)
#: 需要 service / 注册表事实的端点（不参与 snapshot 组合）
REPORT_PATH = reports.PATH
CAPABILITIES_PATH = capabilities.PATH
METRICS_PATH = metrics.PATH
RUNS_PATH = runs.PATH
RUNS_COMPARE_PATH = runs.PATH + runs.COMPARE_SUFFIX


#: path -> 模块（第一版全部为 GET，只读）
ROUTES = {module.PATH: module for module in MODULES}
#: 兼容别名（同一只读切片）
for _module in MODULES:
    for _alias in getattr(_module, "ALIASES", ()):
        ROUTES[_alias] = _module

__all__ = ["MODULES", "ROUTES"]
