"""产品 API 路由表（只读；P0001.10 §控制面边界）。"""

from __future__ import annotations

from api.routes import (
    actions,
    assistant,
    blockers,
    capabilities,
    evidence,
    market_depth,
    market_health,
    market_overlays,
    market_timeline,
    market_trades,
    execution,
    facts,
    market,
    metrics,
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

MODULES = (status, market, prediction, strategy, risk, execution, portfolio, readiness, evidence, blockers)
#: 需要 service / 注册表事实的端点（不参与 snapshot 组合）
REPORT_PATH = reports.PATH
CAPABILITIES_PATH = capabilities.PATH
METRICS_PATH = metrics.PATH
RUNS_PATH = runs.PATH
RUNS_COMPARE_PATH = runs.PATH + runs.COMPARE_SUFFIX
MARKET_TIMELINE_PATH = market_timeline.PATH
MARKET_DEPTH_PATH = market_depth.PATH
MARKET_TRADES_PATH = market_trades.PATH
MARKET_HEALTH_PATH = market_health.PATH
MARKET_OVERLAYS_PATH = market_overlays.PATH
REPLAY_PATH = replay.PATH
FACTS_PATH = facts.PATH
ACTIONS_PATH = actions.PATH
ACTIONS_AUDIT_PATH = actions.AUDIT_PATH
ASSISTANT_CONTEXT_PATH = assistant.PATH
PORTFOLIO_TIMELINE_PATH = portfolio_timeline.PATH


#: path -> 模块（第一版全部为 GET，只读）
ROUTES = {module.PATH: module for module in MODULES}
#: 兼容别名（同一只读切片）
for _module in MODULES:
    for _alias in getattr(_module, "ALIASES", ()):
        ROUTES[_alias] = _module

__all__ = ["MODULES", "ROUTES"]
