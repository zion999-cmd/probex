"""Execution Safety Projection（P0001.13）：把既有事实投影成产品事实 + blocker。

只读；不改变任何交易语义，也不创建第二执行路径。所有阈值来自 `ExecutionSafetyPolicy`。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from market.events.types import Milliseconds

from execution_safety.health import evaluate_health
from execution_safety.latency import BoundedLatencyLog, evaluate_latency
from execution_safety.policy import ExecutionSafetyPolicy
from execution_safety.rate_limit import evaluate_governor
from execution_safety.types import (
    ExecutionHealth,
    ExecutionHealthStatus,
    GovernorState,
    LatencyState,
    ReconciliationState,
    RateLimitStatus,
    VenueLimitState,
)
from execution_safety.venue import VenueRateLimitFacts, venue_facts_age_ms, venue_limit_state
from product.types import BlockerOwner, BlockerSeverity, BlockerView, Fact


@dataclass(slots=True)
class ExecutionSafetyProjection:
    """执行安全投影（provider 全部只读注入；缺 provider ⇒ UNKNOWN）。"""

    clock: Callable[[], Milliseconds]
    policy: ExecutionSafetyPolicy | None = None
    rules_provider: Callable[[], object | None] = lambda: None
    rate_facts_provider: Callable[[], VenueRateLimitFacts | None] = lambda: None
    latency_log: BoundedLatencyLog = field(default_factory=BoundedLatencyLog)
    private_latency_provider: Callable[[], object | None] = lambda: None
    exposure_provider: Callable[[], Mapping[str, object]] = dict
    reconciliation_provider: Callable[[], object | None] = lambda: None
    #: 受控 reconciliation 入口（调用方注入既有 Owner 的入口；未提供 ⇒ Action Manifest 显示无入口）
    reconciliation_requester: Callable[[], object | None] | None = None
    reassessable: bool = False

    # ------------------------------------------------------------------ 事实

    def limits(self) -> VenueLimitState:
        rules = self.rules_provider()
        if rules is None:
            unknown = Fact.unknown("venue trading rules unavailable")
            from execution_safety.types import VenueLimitState as _State

            return _State(tick_size=unknown, step_size=unknown, min_qty=unknown, max_qty=unknown,
                          min_notional=unknown, max_orders=unknown, request_weight_limit=unknown,
                          order_rate_limit=unknown, source="unavailable", observed_at=unknown, age_ms=unknown)
        return venue_limit_state(rules=rules, rate=self.rate_facts_provider(),  # type: ignore[arg-type]
                                 now_ms=int(self.clock()))

    def rate_limits(self) -> GovernorState | None:
        if self.policy is None:
            return None
        return evaluate_governor(policy=self.policy, facts=self.rate_facts_provider(),
                                 now_ms=int(self.clock()))

    def latency(self) -> LatencyState | None:
        if self.policy is None:
            return None
        return evaluate_latency(policy=self.policy, log=self.latency_log, now_ms=int(self.clock()))

    def reconciliation(self) -> ReconciliationState:
        report = self.reconciliation_provider()
        unknown = Fact.unknown("no reconciliation has run in this runtime")
        if report is None:
            required = bool(self.policy is not None
                            and self.policy.reconciliation_required_after_unknown
                            and self._has_unknown_exposure())
            return ReconciliationState(state=Fact.unknown("reconciliation state unavailable"),
                                       last_run_at=unknown, reason=unknown, actions=unknown,
                                       converged=unknown, unknown_count=unknown, lost_count=unknown,
                                       required=required)
        actions = getattr(report, "actions", ()) or ()
        corrective = getattr(report, "corrective_actions", ()) or ()
        return ReconciliationState(
            state=Fact.of("CONVERGED" if bool(getattr(report, "converged", False)) else "NOT_CONVERGED"),
            last_run_at=Fact.of(int(self.clock())),
            reason=Fact.of(str(getattr(report, "reason", "") or "not reported")),
            actions=Fact.of(len(tuple(actions))),
            converged=Fact.of(bool(getattr(report, "converged", False))),
            unknown_count=Fact.of(len(tuple(corrective))),
            lost_count=Fact.of(int(self.exposure().get("lost_count", 0) or 0)),
            required=bool(self.policy is not None
                          and self.policy.reconciliation_required_after_unknown
                          and self._has_unknown_exposure()),
        )

    def exposure(self) -> dict[str, object]:
        provided = dict(self.exposure_provider() or {})
        return {
            "uncertain_exposure": provided.get("uncertain_exposure"),
            "unknown_orders": provided.get("unknown_orders"),
            "lost_count": provided.get("lost_count", 0),
            "rejects": tuple(provided.get("rejects", ()) or ()),
        }

    # ------------------------------------------------------------------ 健康

    def health(self) -> ExecutionHealth:
        governor = self.rate_limits()
        latency = self.latency()
        limits = self.limits()
        reconciliation = self.reconciliation()
        exposure = self.exposure()
        age = venue_facts_age_ms(limits)
        policy = self.policy

        venue_rate_state = "UNKNOWN"
        if policy is not None and age is not None:
            venue_rate_state = "HEALTHY" if age <= int(policy.venue_facts_max_age_ms) else "STALE"
        elif policy is not None and age is None:
            venue_rate_state = "UNAVAILABLE" if limits.request_weight_limit.known is False else "UNKNOWN"

        latency_state = "UNKNOWN" if latency is None else (
            "HEALTHY" if all(stage.status == "OK" for stage in latency.stages)
            else ("UNKNOWN" if all(stage.status == "UNKNOWN" for stage in latency.stages)
                  else "DEGRADED"))
        rate_limit_state = "UNKNOWN" if governor is None else (
            "HEALTHY" if governor.request.status is RateLimitStatus.HEALTHY
            and governor.order.status is RateLimitStatus.HEALTHY
            else ("BLOCKED" if not governor.allows_new_exposure
                  else ("DEGRADED" if any(budget.status is RateLimitStatus.NEAR_LIMIT
                                          for budget in (governor.request, governor.order))
                        else "UNKNOWN")))
        private_latency = self.private_latency_provider()
        private_state = "UNKNOWN" if private_latency is None else str(
            getattr(private_latency, "value", private_latency))
        uncertain = exposure.get("uncertain_exposure")
        unknown_orders = exposure.get("unknown_orders")
        uncertainty_state = "UNKNOWN" if uncertain is None else ("HEALTHY" if float(uncertain) == 0.0
                                                                 else "DEGRADED")
        unknown_orders_state = ("UNKNOWN" if unknown_orders is None
                                else ("HEALTHY" if int(unknown_orders) == 0 else "DEGRADED"))
        reconciliation_state = ("HEALTHY" if reconciliation.state.known
                                and reconciliation.state.value == "CONVERGED"
                                else ("BLOCKED" if reconciliation.required else "UNKNOWN"))
        return evaluate_health(policy=policy, source_status={
            "rate_limit_state": rate_limit_state,
            "venue_facts_state": venue_rate_state,
            "latency_state": latency_state,
            "private_latency": private_state,
            "uncertain_exposure": uncertainty_state,
            "unknown_lost_orders": unknown_orders_state,
            "reconciliation_state": reconciliation_state,
        })

    # ------------------------------------------------------------------ blockers

    def blockers(self) -> tuple[BlockerView, ...]:
        """执行安全类 blocker（新 owner EXECUTION / VENUE；沿用既有 BlockerView 契约）。"""
        views: list[BlockerView] = []
        policy = self.policy
        if policy is None:
            views.append(BlockerView(owner=BlockerOwner.EXECUTION, reason_code="EXECUTION_POLICY_NOT_CONFIGURED",
                                     severity=BlockerSeverity.INFO,
                                     message="execution safety thresholds are not configured",
                                     source_ref="execution.policy"))
            return tuple(views)
        governor = self.rate_limits()
        if governor is not None:
            for budget in (governor.request, governor.order):
                code = {"NEAR_LIMIT": ("RATE_LIMIT_NEAR_LIMIT" if budget.kind == "requests"
                                       else "ORDER_RATE_LIMIT_NEAR_LIMIT"),
                        "EXHAUSTED": ("RATE_LIMIT_EXHAUSTED" if budget.kind == "requests"
                                      else "ORDER_RATE_LIMIT_EXHAUSTED")}.get(budget.status.value)
                if code is None:
                    continue
                severity = (BlockerSeverity.BLOCKING if budget.status is RateLimitStatus.EXHAUSTED
                            else BlockerSeverity.DEGRADED)
                views.append(BlockerView(owner=BlockerOwner.EXECUTION, reason_code=code, severity=severity,
                                         message=f"{budget.kind} budget {budget.status.value} (source={budget.source})",
                                         source_ref=f"execution.rate_limits.{budget.kind}"))
        limits = self.limits()
        age = venue_facts_age_ms(limits)
        if age is None:
            views.append(BlockerView(owner=BlockerOwner.VENUE, reason_code="VENUE_FACTS_UNAVAILABLE",
                                     severity=BlockerSeverity.BLOCKING,
                                     message="rate/order limit facts have no verified venue evidence yet",
                                     source_ref="venue.facts"))
        elif age > int(policy.venue_facts_max_age_ms):
            views.append(BlockerView(owner=BlockerOwner.VENUE, reason_code="VENUE_FACTS_STALE",
                                     severity=BlockerSeverity.DEGRADED,
                                     message=f"venue facts are {age} ms old",
                                     source_ref="venue.facts"))
        latency = self.latency()
        if latency is not None:
            for stage in latency.stages:
                if stage.status == "BUDGET_EXCEEDED":
                    views.append(BlockerView(owner=BlockerOwner.EXECUTION,
                                             reason_code="SUBMIT_LATENCY_BUDGET_EXCEEDED",
                                             severity=BlockerSeverity.DEGRADED,
                                             message=f"{stage.stage} p95 exceeds budget",
                                             source_ref=f"execution.latency.{stage.stage}"))
        exposure = self.exposure()
        uncertain = exposure.get("uncertain_exposure")
        if uncertain is not None and float(uncertain) > 0.0:
            views.append(BlockerView(owner=BlockerOwner.EXECUTION, reason_code="UNCERTAIN_EXPOSURE",
                                     severity=BlockerSeverity.BLOCKING,
                                     message=f"uncertain exposure {uncertain} > 0",
                                     source_ref="execution.exposure.uncertain"))
        reconciliation = self.reconciliation()
        if reconciliation.required:
            views.append(BlockerView(owner=BlockerOwner.EXECUTION, reason_code="RECONCILIATION_REQUIRED",
                                     severity=BlockerSeverity.BLOCKING,
                                     message="unknown exposure requires reconciliation",
                                     source_ref="execution.reconciliation"))
        private = self.private_latency_provider()
        if private is not None and str(getattr(private, "value", private)).upper() not in ("HEALTHY", "OK"):
            views.append(BlockerView(owner=BlockerOwner.EXECUTION, reason_code="PRIVATE_LATENCY_UNHEALTHY",
                                     severity=BlockerSeverity.DEGRADED,
                                     message=f"private latency state is {getattr(private, 'value', private)}",
                                     source_ref="execution.private_latency"))
        health = self.health()
        if health.status is ExecutionHealthStatus.UNKNOWN:
            views.append(BlockerView(owner=BlockerOwner.EXECUTION, reason_code="EXECUTION_HEALTH_UNKNOWN",
                                     severity=BlockerSeverity.INFO,
                                     message="; ".join(health.reasons) or "execution health is UNKNOWN",
                                     source_ref="execution.health"))
        return tuple(views)

    # ------------------------------------------------------------------ 内部

    def _has_unknown_exposure(self) -> bool:
        exposure = self.exposure()
        uncertain = exposure.get("uncertain_exposure")
        return uncertain is not None and float(uncertain) > 0.0


__all__ = ["ExecutionSafetyProjection"]
