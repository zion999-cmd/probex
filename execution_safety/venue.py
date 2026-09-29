"""Venue Limit Facts（P0001.13 §2）。

- 价格/数量规则：**复用** `TradingRules`（与 normalization 同一事实源，SC-1）；
- rate/order 限额：**必须来自真实响应证据**；未接入 ⇒ `UNKNOWN`（SC-2，不凭文档猜）；
- `age_ms` / `source` 一并暴露，供 freshness 判定（阈值来自 policy）。
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import Milliseconds

from connectors.binance.market_data.exchange_info import TradingRules
from product.types import Fact


@dataclass(frozen=True, slots=True)
class VenueRateLimitFacts:
    """rate/order 限额的真实事实（由 connector 在**证据确认后**提供）。

    `source` 必须写明事实来源（例如某个响应头字段或 exchangeInfo 字段），不得写"unknown"以外的占位描述。
    """

    request_weight_limit: int | None
    order_rate_limit: int | None
    window_ms: int | None
    used_weight: int | None
    used_orders: int | None
    reset_at_ms: Milliseconds | None
    source: str
    observed_at_ms: Milliseconds

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("VenueRateLimitFacts.source must be a non-empty string")


@dataclass(frozen=True, slots=True)
class VenueLimitFacts:
    """产品化的 venue 事实（未接入 rate/order 限额时对应字段为 UNKNOWN）。"""

    rules: TradingRules
    rate: VenueRateLimitFacts | None
    now_ms: Milliseconds

    @property
    def max_orders(self) -> Fact:
        # 交易所未提供 ⇒ UNKNOWN（不推测）
        return Fact.unknown("venue did not report a max order count")


def venue_limit_state(*, rules: TradingRules, rate: VenueRateLimitFacts | None,
                      now_ms: Milliseconds) -> "VenueLimitState":
    """把既有交易所事实映射成 VenueLimitState（纯搬运；不做任何推导）。"""
    from execution_safety.types import VenueLimitState

    if not isinstance(rules, TradingRules):
        raise ValueError("venue_limit_state requires TradingRules")
    rate_unknown = "no real venue evidence for rate/order limits yet"
    source = "exchangeInfo.filters" if rate is None else f"{rate.source}"
    observed_at = None if rate is None else rate.observed_at_ms
    return VenueLimitState(
        tick_size=Fact.of(rules.tick_size),
        step_size=Fact.of(rules.step_size),
        min_qty=Fact.of(rules.min_qty),
        max_qty=Fact.of(rules.max_qty),
        min_notional=Fact.of(rules.min_notional),
        max_orders=Fact.unknown("venue did not report a max order count"),
        request_weight_limit=(Fact.unknown(rate_unknown) if rate is None or rate.request_weight_limit is None
                              else Fact.of(rate.request_weight_limit)),
        order_rate_limit=(Fact.unknown(rate_unknown) if rate is None or rate.order_rate_limit is None
                          else Fact.of(rate.order_rate_limit)),
        source=source,
        observed_at=(Fact.unknown("rate/order limits not observed") if observed_at is None
                     else Fact.of(int(observed_at))),
        age_ms=(Fact.unknown(rate_unknown) if observed_at is None
                else Fact.of(int(now_ms) - int(observed_at))),
    )


def venue_facts_age_ms(state: "VenueLimitState") -> int | None:
    """freshness 事实（未知 ⇒ None，由 projection 映射为 UNKNOWN）。"""
    return None if not state.age_ms.known else int(state.age_ms.value)


__all__ = ["VenueLimitFacts", "VenueRateLimitFacts", "venue_facts_age_ms", "venue_limit_state"]
