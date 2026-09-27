"""已实现波动率。

```text
realized_volatility_h = sqrt( Σ r_i² / (h / 1000) )
```

其中 `r_i` 是窗口 `[t - h, t]` 内相邻 mid 观测的对数收益，`h / 1000` 为窗口秒数。

时间尺度必须明确：这里的量纲是「每 √秒」，因此结果**不随 event frequency 改变量纲**
（观测更密时每段收益更小，平方和按秒归一后量级不变）。
不使用 per-observation sigma × seconds 这类量纲混用。

可用性：窗口未被历史覆盖（`t - h` 之前没有观测）或窗内观测少于 2 个 → `None`。
"""

from __future__ import annotations

import math

from market.events.types import Milliseconds
from market.features.returns import log_returns
from market.features.windows import TimeSeries
from market.state.types import VolatilityFeatures

#: 固定波动率窗口（秒级定义）。
VOLATILITY_WINDOWS_MS: tuple[int, ...] = (5_000, 15_000, 30_000, 60_000)

UNAVAILABLE_VOLATILITY = VolatilityFeatures(
    realized_volatility_5s=None,
    realized_volatility_15s=None,
    realized_volatility_30s=None,
    realized_volatility_60s=None,
)


def realized_volatility(history: TimeSeries, *, at: Milliseconds, window_ms: int) -> float | None:
    """单个窗口的每秒已实现波动率。"""
    oldest = history.oldest_timestamp
    if oldest is None or oldest > at - window_ms:
        return None

    returns = log_returns(history.observations_between(at - window_ms, at))
    if returns is None:
        return None

    sum_of_squares = math.fsum(value * value for value in returns)
    return math.sqrt(sum_of_squares / (window_ms / 1_000))


def compute_volatility(history: TimeSeries, *, at: Milliseconds) -> VolatilityFeatures:
    """计算全部波动率窗口。"""
    return VolatilityFeatures(
        realized_volatility_5s=realized_volatility(history, at=at, window_ms=5_000),
        realized_volatility_15s=realized_volatility(history, at=at, window_ms=15_000),
        realized_volatility_30s=realized_volatility(history, at=at, window_ms=30_000),
        realized_volatility_60s=realized_volatility(history, at=at, window_ms=60_000),
    )
