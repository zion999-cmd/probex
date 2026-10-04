"""产品 API 路由表（只读；P0001.10 §控制面边界）。"""

from __future__ import annotations

from api.routes import (
    actions,
    assistant,
    blockers,
    capabilities,
    evidence,
    market_candles,
    market_depth,
    market_health,
    market_overlays,
    market_timeline,
    market_trades,
    execution,
    execution_safety,
    instrument,
    facts,
    market,
    metrics,
    ops,
    portfolio,
    portfolio_timeline,
    prediction,
    readiness,
    replay,
    reports,
    runs,
    risk,
    status,
    strategy,
)

#: `Decision → Order(s)` 反查端点（P0001.15 §15 / SC-27；带 query 参数，由 server 直接处理）
DECISION_ORDERS_PATH = "/api/v1/decisions/orders"

MODULES = (status, market, prediction, strategy, risk, execution, portfolio, readiness, evidence, instrument,
           blockers, ops)
#: 需要 service / 注册表事实的端点（不参与 snapshot 组合）
REPORT_PATH = reports.PATH
CAPABILITIES_PATH = capabilities.PATH
METRICS_PATH = metrics.PATH
RUNS_PATH = runs.PATH
RUNS_COMPARE_PATH = runs.PATH + runs.COMPARE_SUFFIX
MARKET_TIMELINE_PATH = market_timeline.PATH
MARKET_CANDLES_PATH = market_candles.PATH
MARKET_DEPTH_PATH = market_depth.PATH
MARKET_TRADES_PATH = market_trades.PATH
MARKET_HEALTH_PATH = market_health.PATH
MARKET_OVERLAYS_PATH = market_overlays.PATH
REPLAY_PATH = replay.PATH
FACTS_PATH = facts.PATH
ACTIONS_PATH = actions.PATH
ACTIONS_AUDIT_PATH = actions.AUDIT_PATH
ASSISTANT_CONTEXT_PATH = assistant.PATH
ASSISTANT_EXPLAIN_PATH = assistant.EXPLAIN_PATH
EXECUTION_SUB_PATHS = execution_safety.SUB_PATHS
PORTFOLIO_TIMELINE_PATH = portfolio_timeline.PATH
OPS_PATH = ops.PATH


#: path -> 模块（第一版全部为 GET，只读）
ROUTES = {module.PATH: module for module in MODULES}
#: 带 query 参数、由 server 直接处理的只读端点（不是 snapshot 切片）
QUERY_ROUTES = (DECISION_ORDERS_PATH, ASSISTANT_CONTEXT_PATH, ASSISTANT_EXPLAIN_PATH)
#: 全部已注册只读路径（snapshot 切片 + 别名 + query 端点）；UI 只允许引用这些路径
READ_PATHS = tuple(ROUTES) + QUERY_ROUTES
#: 兼容别名（同一只读切片）
for _module in MODULES:
    for _alias in getattr(_module, "ALIASES", ()):
        ROUTES[_alias] = _module

__all__ = ["DECISION_ORDERS_PATH", "MODULES", "QUERY_ROUTES", "READ_PATHS", "ROUTES"]
