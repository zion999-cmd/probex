"""多档深度与失衡 feature。

档位在 schema 中固定为 1 / 5 / 10 / 20（P0001.3 §4），不允许运行时任意变化。

```text
bid_depth_N      = 前 N 档 bid size 之和（档位不足 N 时取现有档位之和）
ask_depth_N      = 前 N 档 ask size 之和
DepthImbalance(N)= (bid_depth_N - ask_depth_N) / (bid_depth_N + ask_depth_N)   ∈ [-1, 1]
L1Imbalance      = DepthImbalance(1)
vamp             = VAMP_5 = Σ(price * size) / Σ(size)   仅取双边前 5 档
```

缺失语义：盘口不可用 → `None`；分母为 0 → `None`；某一侧无档位时该侧深度为 0.0
（这是「确实观测到 0」，与「不可用」不同）。
"""

from __future__ import annotations

import math

from market.book.market_book import BookView
from market.events.payloads import PriceLevel
from market.state.types import DepthFeatures

#: 固定档位集合。
DEPTH_LEVELS: tuple[int, ...] = (1, 5, 10, 20)

#: VAMP 使用的档位数。
VAMP_LEVELS = 5

UNAVAILABLE_DEPTH_FEATURES = DepthFeatures(
    bid_depth_1=None,
    bid_depth_5=None,
    bid_depth_10=None,
    bid_depth_20=None,
    ask_depth_1=None,
    ask_depth_5=None,
    ask_depth_10=None,
    ask_depth_20=None,
    l1_imbalance=None,
    depth_imbalance_1=None,
    depth_imbalance_5=None,
    depth_imbalance_10=None,
    depth_imbalance_20=None,
    vamp=None,
)


def imbalance(bid_size: float, ask_size: float) -> float | None:
    """标准化失衡 `(B - A) / (B + A)`；分母为 0 时返回 `None`。"""
    total = bid_size + ask_size
    if total == 0.0:
        return None
    return (bid_size - ask_size) / total


def _depth_sum(levels: tuple[PriceLevel, ...], n: int) -> float:
    return math.fsum(level.size for level in levels[:n])


def vamp(levels_by_side: tuple[tuple[PriceLevel, ...], ...], n: int = VAMP_LEVELS) -> float | None:
    """VAMP：双边前 N 档的成交量加权中间价；总数量为 0 时返回 `None`。"""
    selected = [level for levels in levels_by_side for level in levels[:n]]
    total_size = math.fsum(level.size for level in selected)
    if total_size == 0.0:
        return None
    return math.fsum(level.price * level.size for level in selected) / total_size


def compute_depth_features(view: BookView | None) -> DepthFeatures:
    """由盘口视图计算深度 feature；`None` 表示盘口不可用。"""
    if view is None:
        return UNAVAILABLE_DEPTH_FEATURES

    bid_depths = {n: _depth_sum(view.bids, n) for n in DEPTH_LEVELS}
    ask_depths = {n: _depth_sum(view.asks, n) for n in DEPTH_LEVELS}
    return DepthFeatures(
        bid_depth_1=bid_depths[1],
        bid_depth_5=bid_depths[5],
        bid_depth_10=bid_depths[10],
        bid_depth_20=bid_depths[20],
        ask_depth_1=ask_depths[1],
        ask_depth_5=ask_depths[5],
        ask_depth_10=ask_depths[10],
        ask_depth_20=ask_depths[20],
        l1_imbalance=imbalance(bid_depths[1], ask_depths[1]),
        depth_imbalance_1=imbalance(bid_depths[1], ask_depths[1]),
        depth_imbalance_5=imbalance(bid_depths[5], ask_depths[5]),
        depth_imbalance_10=imbalance(bid_depths[10], ask_depths[10]),
        depth_imbalance_20=imbalance(bid_depths[20], ask_depths[20]),
        vamp=vamp((view.bids, view.asks)),
    )
