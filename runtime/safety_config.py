"""Deployment config → 既有 execution_safety 契约（closure Slice 3，Step 1/2）。

只做**解析与构造**：不实现任何 health / latency / fact 逻辑，全部复用 P0001.13 已定义的类型。
没有配置就不构造（保持 UNKNOWN），**不发明任何数值**。
"""

from __future__ import annotations

from typing import Any


def build_safety_policy(values: dict[str, Any]) -> object | None:
    """从 `safety.*` 构造 `ExecutionSafetyPolicy`；缺任一必需键 ⇒ None。"""
    from execution_safety.policy import ExecutionSafetyPolicy, HealthRule
    from execution_safety.types import HEALTH_SOURCES, LATENCY_STAGES, Severity

    required = ("safety.near_limit_ratio", "safety.exhausted_ratio", "safety.order_rate_near_limit_ratio",
                "safety.venue_facts_max_age_ms", "safety.latency_min_samples", "safety.latency_window_ms",
                "safety.reconciliation_required_after_unknown", "safety.latency_budget_ms",
                "safety.health_rules")
    if any(key not in values for key in required):
        return None
    budgets = values["safety.latency_budget_ms"]
    if not isinstance(budgets, dict) or any(stage not in budgets for stage in LATENCY_STAGES):
        return None
    raw_rules = values["safety.health_rules"]
    if not isinstance(raw_rules, dict):
        return None
    rules: dict[str, HealthRule] = {}
    for source in HEALTH_SOURCES:
        spec = raw_rules.get(source)
        if not isinstance(spec, dict) or not {"severity", "downgrades", "required"} <= set(spec):
            return None
        rules[source] = HealthRule(severity=Severity(str(spec["severity"]).upper()),
                                   downgrades=bool(spec["downgrades"]), required=bool(spec["required"]))
    return ExecutionSafetyPolicy(
        near_limit_ratio=float(values["safety.near_limit_ratio"]),
        exhausted_ratio=float(values["safety.exhausted_ratio"]),
        order_rate_near_limit_ratio=float(values["safety.order_rate_near_limit_ratio"]),
        venue_facts_max_age_ms=int(values["safety.venue_facts_max_age_ms"]),
        latency_budget_ms={stage: int(value) for stage, value in budgets.items()},
        latency_min_samples=int(values["safety.latency_min_samples"]),
        latency_window_ms=int(values["safety.latency_window_ms"]),
        reconciliation_required_after_unknown=bool(values["safety.reconciliation_required_after_unknown"]),
        health_rules=rules)


RULE_KEYS = ("venue.rules.tick_size", "venue.rules.step_size", "venue.rules.min_qty",
             "venue.rules.max_qty", "venue.rules.min_notional",
             "venue.rules.min_price", "venue.rules.max_price")


def build_trading_rules(values: dict[str, Any], *, symbol: str) -> object | None:
    """从 `venue.rules.*`（真实 exchangeInfo 证据）构造既有 `TradingRules`；缺键 ⇒ None。"""
    if any(key not in values for key in RULE_KEYS):
        return None
    from connectors.binance.market_data.exchange_info import TradingRules

    return TradingRules(symbol=symbol, status="TRADING",
                        tick_size=float(values["venue.rules.tick_size"]),
                        min_price=float(values["venue.rules.min_price"]),
                        max_price=float(values["venue.rules.max_price"]),
                        step_size=float(values["venue.rules.step_size"]),
                        min_qty=float(values["venue.rules.min_qty"]),
                        max_qty=float(values["venue.rules.max_qty"]),
                        min_notional=float(values["venue.rules.min_notional"]))


def build_limit_definition(values: dict[str, Any]) -> object | None:
    """**限额定义**（来自 exchangeInfo/rateLimits 类事实）；无证据键 ⇒ None。"""
    keys = ("venue.definition.request_weight_limit", "venue.definition.order_rate_limit",
            "venue.definition.max_orders", "venue.definition.window_ms")
    if not any(key in values for key in keys):
        return None
    from execution_safety.venue import VenueLimitDefinition

    return VenueLimitDefinition(
        request_weight_limit=(int(values["venue.definition.request_weight_limit"])
                              if "venue.definition.request_weight_limit" in values else None),
        order_rate_limit=(int(values["venue.definition.order_rate_limit"])
                          if "venue.definition.order_rate_limit" in values else None),
        max_orders=(int(values["venue.definition.max_orders"])
                    if "venue.definition.max_orders" in values else None),
        window_ms=(int(values["venue.definition.window_ms"])
                   if "venue.definition.window_ms" in values else None),
        source=str(values.get("venue.definition.source") or "exchangeInfo.rateLimits"))


def build_usage_snapshot(values: dict[str, Any]) -> object | None:
    """**当前用量**（来自真实响应 header / use counters）；无证据键 ⇒ None。"""
    keys = ("venue.usage.used_weight", "venue.usage.used_orders")
    if not any(key in values for key in keys):
        return None
    from execution_safety.venue import VenueUsageSnapshot

    return VenueUsageSnapshot(
        used_weight=(int(values["venue.usage.used_weight"]) if "venue.usage.used_weight" in values else None),
        used_orders=(int(values["venue.usage.used_orders"]) if "venue.usage.used_orders" in values else None),
        reset_at_ms=(int(values["venue.usage.reset_at_ms"]) if "venue.usage.reset_at_ms" in values else None),
        source=str(values.get("venue.usage.source") or "response.headers"))


__all__ = ["RULE_KEYS", "build_limit_definition", "build_safety_policy", "build_trading_rules",
           "build_usage_snapshot"]
