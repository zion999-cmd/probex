"""`ExecutionSafetyPolicy`（P0001.13 §1）：**所有业务阈值显式必填，无默认值**。

本模块不提供任何默认值，也不内置任何"经验数值"。缺 policy / 缺字段 ⇒ 调用方（projection）
必须给出 `UNKNOWN` 与 `EXECUTION_POLICY_NOT_CONFIGURED`，不得猜测。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from execution_safety.types import HEALTH_SOURCES, LATENCY_STAGES, Severity


class ExecutionSafetyPolicyError(ValueError):
    """policy 契约错误（缺字段 / 数值非法 / 覆盖不全）。"""


@dataclass(frozen=True, slots=True)
class HealthRule:
    """单个健康来源的合成规则（severity + 是否参与降级 + 是否必需）。"""

    severity: Severity
    downgrades: bool
    required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.severity, Severity):
            raise ExecutionSafetyPolicyError("HealthRule.severity must be a Severity")
        for name in ("downgrades", "required"):
            if not isinstance(getattr(self, name), bool):
                raise ExecutionSafetyPolicyError(f"HealthRule.{name} must be a bool")


@dataclass(frozen=True, slots=True)
class ExecutionSafetyPolicy:
    """执行安全阈值（显式注入；**无默认值**）。"""

    near_limit_ratio: float
    exhausted_ratio: float
    order_rate_near_limit_ratio: float
    venue_facts_max_age_ms: int
    latency_budget_ms: Mapping[str, int]
    latency_min_samples: int
    latency_window_ms: int
    reconciliation_required_after_unknown: bool
    health_rules: Mapping[str, HealthRule]

    def __post_init__(self) -> None:
        for name in ("near_limit_ratio", "exhausted_ratio", "order_rate_near_limit_ratio"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) < 1.0:
                raise ExecutionSafetyPolicyError(f"{name} must be a ratio in (0, 1), got {value!r}")
        if not float(self.exhausted_ratio) < float(self.near_limit_ratio):
            raise ExecutionSafetyPolicyError(
                "exhausted_ratio must be strictly below near_limit_ratio (both are explicit policy values)"
            )
        for name in ("venue_facts_max_age_ms", "latency_min_samples", "latency_window_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExecutionSafetyPolicyError(f"{name} must be a positive int, got {value!r}")
        if not isinstance(self.reconciliation_required_after_unknown, bool):
            raise ExecutionSafetyPolicyError("reconciliation_required_after_unknown must be a bool")
        budgets = dict(self.latency_budget_ms or {})
        missing = [stage for stage in LATENCY_STAGES if stage not in budgets]
        if missing:
            raise ExecutionSafetyPolicyError(f"latency_budget_ms is missing stages: {missing}")
        extra = [stage for stage in budgets if stage not in LATENCY_STAGES]
        if extra:
            raise ExecutionSafetyPolicyError(f"latency_budget_ms has unknown stages: {extra}")
        for stage, value in budgets.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExecutionSafetyPolicyError(f"latency_budget_ms[{stage}] must be a positive int")
        rules = dict(self.health_rules or {})
        missing_rules = [source for source in HEALTH_SOURCES if source not in rules]
        if missing_rules:
            raise ExecutionSafetyPolicyError(f"health_rules is missing sources: {missing_rules}")
        for source, rule in rules.items():
            if not isinstance(rule, HealthRule):
                raise ExecutionSafetyPolicyError(f"health_rules[{source}] must be a HealthRule")
        object.__setattr__(self, "latency_budget_ms", MappingProxyType(budgets))
        object.__setattr__(self, "health_rules", MappingProxyType(rules))

    @property
    def required_sources(self) -> tuple[str, ...]:
        return tuple(source for source, rule in self.health_rules.items() if rule.required)


__all__ = ["ExecutionSafetyPolicy", "ExecutionSafetyPolicyError", "HealthRule"]
