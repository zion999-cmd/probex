"""mid 收益窗口。

```text
return_h = mid(t) / mid(latest observation <= t - h) - 1
```

lookup 规则（P0001.3 §7）：取「目标时刻或更早」的最近一次观测；**永不读取未来数据**。
事件驱动数据不保证恰好存在 `t - h` 的观测，因此收益的时间基准是 `t - h` 的上一次观测。
历史不足时返回 `None`，不是 0。
"""

from __future__ import annotations

import math

from market.events.types import Milliseconds
from market.features.windows import TimeSeries
from market.state.types import ReturnsFeatures

#: 固定收益窗口。
RETURN_WINDOWS_MS: tuple[int, ...] = (1_000, 3_000, 5_000, 15_000, 30_000, 60_000, 300_000)

#: 最长窗口，同时作为 `history_ready` 的覆盖度阈值。
HISTORY_WINDOW_MS: Milliseconds = max(RETURN_WINDOWS_MS)

#: 价格历史保留时长：最长窗口的两倍。
PRICE_HISTORY_HORIZON_MS: Milliseconds = 2 * HISTORY_WINDOW_MS

UNAVAILABLE_RETURNS = ReturnsFeatures(
    return_1s=None,
    return_3s=None,
    return_5s=None,
    return_15s=None,
    return_30s=None,
    return_60s=None,
    return_300s=None,
)


def compute_returns(
    history: TimeSeries,
    *,
    at: Milliseconds,
    current_mid: float | None,
) -> ReturnsFeatures:
    """计算多窗口收益；`current_mid` 为 `None` 时全部 unavailable。"""
    if current_mid is None or current_mid == 0.0:
        return UNAVAILABLE_RETURNS

    def window(window_ms: int) -> float | None:
        past = history.value_at_or_before(at - window_ms)
        if past is None or past == 0.0:
            return None
        return current_mid / past - 1.0

    return ReturnsFeatures(
        return_1s=window(1_000),
        return_3s=window(3_000),
        return_5s=window(5_000),
        return_15s=window(15_000),
        return_30s=window(30_000),
        return_60s=window(60_000),
        return_300s=window(300_000),
    )


def log_returns(values: tuple[float, ...]) -> tuple[float, ...] | None:
    """相邻观测的对数收益；出现非正价格时返回 `None`。"""
    if len(values) < 2:
        return None
    returns = []
    for previous, current in zip(values, values[1:]):
        if previous <= 0.0 or current <= 0.0:
            return None
        returns.append(math.log(current / previous))
    return tuple(returns)
