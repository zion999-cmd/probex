"""Execution Health（P0001.13 §5）：四态统一投影，**不替代 Readiness**。

规则（全部来自 policy，无一硬编码）：

- 每个来源按 `health_rules[source].severity` / `.downgrades` / `.required` 参与合成；
- **任何 required 来源为 UNKNOWN ⇒ 不得 HEALTHY**（SC-3）；
- 降级来源可解释（返回 `reasons`，SC-8）；
- policy 缺失 ⇒ `UNKNOWN` + `EXECUTION_POLICY_NOT_CONFIGURED`（由 projection 负责产出 blocker）。
"""

from __future__ import annotations

from collections.abc import Mapping

from execution_safety.policy import ExecutionSafetyPolicy
from execution_safety.types import ExecutionHealth, ExecutionHealthStatus, HEALTH_SOURCES, Severity


def _normalise(source_status: Mapping[str, str]) -> dict[str, str]:
    return {source: str(source_status.get(source, "UNKNOWN")).upper() for source in HEALTH_SOURCES}


def evaluate_health(*, policy: ExecutionSafetyPolicy | None,
                    source_status: Mapping[str, str]) -> ExecutionHealth:
    """合成 ExecutionHealth（policy 为 None ⇒ UNKNOWN，绝不 green）。"""
    statuses = _normalise(source_status)
    if policy is None:
        return ExecutionHealth(status=ExecutionHealthStatus.UNKNOWN,
                               reasons=("EXECUTION_POLICY_NOT_CONFIGURED",),
                               source_status=statuses)

    reasons: list[str] = []
    blocking = False
    degraded = False
    unknown_required = False
    for source, status in statuses.items():
        rule = policy.health_rules[source]
        if status == "UNKNOWN":
            if rule.required:
                unknown_required = True
                reasons.append(f"{source}:UNKNOWN(required)")
            else:
                reasons.append(f"{source}:UNKNOWN")
            continue
        if not rule.downgrades:
            continue
        if status == "HEALTHY":
            continue
        reasons.append(f"{source}:{status}")
        if rule.severity is Severity.BLOCKING:
            blocking = True
        elif rule.severity is Severity.DEGRADED:
            degraded = True
    if blocking:
        status = ExecutionHealthStatus.BLOCKED
    elif degraded:
        status = ExecutionHealthStatus.DEGRADED
    elif unknown_required:
        status = ExecutionHealthStatus.UNKNOWN
    else:
        status = ExecutionHealthStatus.HEALTHY
    return ExecutionHealth(status=status, reasons=tuple(reasons), source_status=statuses)


__all__ = ["evaluate_health"]
