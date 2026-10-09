"""服务端指标计算（P0001.17 §5）：**不在 UI 计算业务指标**。

当前只提供 ATR（Average True Range）：这是**标准定义**（Wilder 平滑），输入是既有 candle 聚合器产出的
真实 OHLC 事实，周期显式给出（无默认业务值）。VWAP 不在这里——它由 candle 聚合器直接用**真实成交**
逐桶计算（见 `product/candles.py::aggregate_candles`）。

输出与输入等长：不足周期的位置为 `None`（UNKNOWN，不是 0 也不是外推）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from market.events.types import Milliseconds


class IndicatorError(ValueError):
    """指标计算契约错误。"""


@dataclass(frozen=True, slots=True)
class AtrPoint:
    ts: Milliseconds
    #: Wilder ATR；不足周期 ⇒ None（UNKNOWN）
    atr: float | None


def wilder_atr(candles: Sequence[object], *, period: int) -> tuple[AtrPoint, ...]:
    """Wilder ATR（真波幅的 Wilder 平滑）。

    - 真波幅 `TR = max(high-low, |high-prev_close|, |low-prev_close|)`（首根用 high-low）；
    - 第 `period` 根起 `ATR = (prev_ATR * (period-1) + TR) / period`（前 `period-1` 根为 None）。
    """
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise IndicatorError("wilder_atr requires a positive int period")
    points: list[AtrPoint] = []
    prev_close: float | None = None
    atr: float | None = None
    tr_window: list[float] = []
    for candle in candles:
        high = float(getattr(candle, "high"))
        low = float(getattr(candle, "low"))
        ts = int(getattr(candle, "ts"))
        true_range = high - low
        if prev_close is not None:
            true_range = max(true_range, abs(high - prev_close), abs(low - prev_close))
        prev_close = float(getattr(candle, "close"))
        if atr is None:
            tr_window.append(true_range)
            if len(tr_window) == period:
                atr = sum(tr_window) / period          # 首个 ATR = 前 period 个 TR 的简单平均（Wilder 首值）
        else:
            atr = (atr * (period - 1) + true_range) / period
        points.append(AtrPoint(ts=ts, atr=None if (atr is None) else round(atr, 8)))
    return tuple(points)


__all__ = ["AtrPoint", "IndicatorError", "wilder_atr"]
