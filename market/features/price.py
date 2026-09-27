"""最优档与价格派生 feature。

公式固定（P0001.3 §1.3）：

```text
mid          = (best_bid + best_ask) / 2
spread       = best_ask - best_bid
spread_bps   = spread / mid * 10000
microprice   = (best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)
```

缺失语义：`view` 为 `None`（盘口不可用）或任一侧缺档 → 对应 feature 为 `None`。
`bid_size + ask_size == 0` 时 `microprice` 为 `None`，不是 0。
"""

from __future__ import annotations

from market.book.market_book import BookView
from market.state.types import PriceFeatures

UNAVAILABLE_PRICE_FEATURES = PriceFeatures(
    best_bid=None,
    best_ask=None,
    bid_size=None,
    ask_size=None,
    mid=None,
    spread=None,
    spread_bps=None,
    microprice=None,
)


def microprice(best_bid: float, bid_size: float, best_ask: float, ask_size: float) -> float | None:
    """成交量加权最优价：本侧价格乘以对侧数量。"""
    total = bid_size + ask_size
    if total == 0.0:
        return None
    return (best_bid * ask_size + best_ask * bid_size) / total


def compute_price_features(view: BookView | None) -> PriceFeatures:
    """由盘口视图计算价格 feature；`None` 表示盘口不可用。"""
    if view is None:
        return UNAVAILABLE_PRICE_FEATURES

    best_bid = view.bids[0] if view.bids else None
    best_ask = view.asks[0] if view.asks else None
    bid_size = best_bid.size if best_bid is not None else None
    ask_size = best_ask.size if best_ask is not None else None

    mid = None
    spread = None
    spread_bps = None
    micro = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid.price + best_ask.price) / 2
        spread = best_ask.price - best_bid.price
        if mid != 0.0:
            spread_bps = spread / mid * 10_000
        micro = microprice(best_bid.price, best_bid.size, best_ask.price, best_ask.size)

    return PriceFeatures(
        best_bid=best_bid.price if best_bid is not None else None,
        best_ask=best_ask.price if best_ask is not None else None,
        bid_size=bid_size,
        ask_size=ask_size,
        mid=mid,
        spread=spread,
        spread_bps=spread_bps,
        microprice=micro,
    )
