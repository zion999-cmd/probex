"""RateLimitGovernor（P0001.13 §3）。

Probex 自有的**门与事实**，不是重试器：

- 只输出 request/order budget 的事实与是否允许**新增暴露**；
- **没有任何 retry 逻辑**（SC-4：不因限流触发 submit 重试）；
- 降险（cancel / reconciliation）**永不被普通限流逻辑阻断**（SC-19）；
- 未取得真实 venue 事实 ⇒ `UNKNOWN`（不猜，不填 0）。
"""

from __future__ import annotations

from market.events.types import Milliseconds

from execution_safety.policy import ExecutionSafetyPolicy
from execution_safety.types import BudgetState, GovernorState, RateLimitStatus
from execution_safety.venue import VenueRateLimitFacts
from product.types import Fact

REQUESTS = "requests"
ORDERS = "orders"


def _status(*, used: int | None, limit: int | None, ratio: float, exhausted: float) -> RateLimitStatus:
    if used is None or limit is None or limit <= 0:
        return RateLimitStatus.UNKNOWN
    remaining_ratio = max(0.0, (limit - used) / limit)
    if remaining_ratio <= exhausted:
        return RateLimitStatus.EXHAUSTED
    if remaining_ratio <= ratio:
        return RateLimitStatus.NEAR_LIMIT
    return RateLimitStatus.HEALTHY


def _budget(kind: str, *, used: int | None, limit: int | None, reset_at: Milliseconds | None,
            window_ms: int | None, source: str, ratio: float, exhausted: float) -> BudgetState:
    unknown = Fact.unknown("venue did not report this budget")
    status = _status(used=used, limit=limit, ratio=ratio, exhausted=exhausted)
    return BudgetState(
        kind=kind,
        status=status,
        limit=(unknown if limit is None else Fact.of(int(limit))),
        used=(unknown if used is None else Fact.of(int(used))),
        remaining=(unknown if (limit is None or used is None) else Fact.of(max(0, int(limit) - int(used)))),
        reset_at=(Fact.unknown("venue did not report a reset time") if reset_at is None
                  else Fact.of(int(reset_at))),
        source=source,
        window_ms=(unknown if window_ms is None else Fact.of(int(window_ms))),
    )


def evaluate_governor(*, policy: ExecutionSafetyPolicy, facts: VenueRateLimitFacts | None,
                      now_ms: Milliseconds) -> GovernorState:
    """计算 request/order budget 状态（只读；不做任何重试或提交动作）。"""
    if not isinstance(policy, ExecutionSafetyPolicy):
        raise ValueError("evaluate_governor requires an ExecutionSafetyPolicy")
    source = "unknown" if facts is None else facts.source
    request = _budget(REQUESTS, used=None if facts is None else facts.used_weight,
                      limit=None if facts is None else facts.request_weight_limit,
                      reset_at=None if facts is None else facts.reset_at_ms,
                      window_ms=None if facts is None else facts.window_ms, source=source,
                      ratio=float(policy.near_limit_ratio), exhausted=float(policy.exhausted_ratio))
    order = _budget(ORDERS, used=None if facts is None else facts.used_orders,
                    limit=None if facts is None else facts.order_rate_limit,
                    reset_at=None if facts is None else facts.reset_at_ms,
                    window_ms=None if facts is None else facts.window_ms, source=source,
                    ratio=float(policy.order_rate_near_limit_ratio),
                    exhausted=float(policy.exhausted_ratio))
    reasons = [f"{budget.kind}:{budget.status.value}" for budget in (request, order)
               if budget.status in (RateLimitStatus.NEAR_LIMIT, RateLimitStatus.EXHAUSTED,
                                    RateLimitStatus.UNKNOWN)]
    exhausted = any(budget.status is RateLimitStatus.EXHAUSTED for budget in (request, order))
    return GovernorState(
        request=request,
        order=order,
        # 只有 EXHAUSTED 才阻止"新增暴露"；NEAR_LIMIT 只降级健康度
        allows_new_exposure=not exhausted,
        # 降险永不被普通限流锁死（SC-19）
        allows_de_risking=True,
        reasons=tuple(reasons),
    )


__all__ = ["ORDERS", "REQUESTS", "evaluate_governor"]
