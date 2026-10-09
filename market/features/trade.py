"""成交域特征累积（P0001.17 授权补齐）：让 `TradeFeatures` 不再恒为 unavailable。

Owner 边界：本模块是 `FeatureEngine` 的**内部状态**（唯一 Owner 是 engine），只把真实 `TradePayload`
事实累积成窗口统计；`MarketState` 仍是不可变快照。

窗口语义（显式、可审计）：`window_ms` 由构造方给出（engine 使用与最大收益窗口一致的 300_000 ms），
窗口内没有成交 ⇒ 相关量为 `None`（UNKNOWN，不是 0）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from market.events.payloads import AggressorSide, TradePayload
from market.events.types import Milliseconds
from market.state.types import TradeFeatures


@dataclass
class TradeFeatureAccumulator:
    """按时间窗累积真实成交事实（aggressor 方向 + 数量 + 价格）。"""

    window_ms: Milliseconds
    # (ts, price, quantity, is_buy_aggressor)
    _prints: list[tuple[Milliseconds, float, float, bool]] = field(default_factory=list)
    _total_count: int = field(default=0, init=False)
    _total_volume: float = field(default=0.0, init=False)
    _first_ts: Milliseconds | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if isinstance(self.window_ms, bool) or not isinstance(self.window_ms, int) or self.window_ms <= 0:
            raise ValueError("TradeFeatureAccumulator.window_ms must be a positive int")

    # ------------------------------------------------------------------ 输入

    def on_trade(self, payload: TradePayload, *, timestamp: Milliseconds) -> None:
        """记录一条真实成交（`TradePayload` 已在构造期完成不变量校验）。"""
        if not isinstance(payload, TradePayload):
            raise ValueError("on_trade requires a TradePayload")
        is_buy = payload.aggressor is AggressorSide.BUY
        self._prints.append((int(timestamp), float(payload.price), float(payload.quantity), is_buy))
        self._total_count += 1
        self._total_volume += float(payload.quantity)
        if self._first_ts is None:
            self._first_ts = int(timestamp)
        self._prune(at=int(timestamp))

    def _prune(self, *, at: Milliseconds) -> None:
        floor = at - self.window_ms
        # 成交按时间单调到达（market 事件契约），因此只需从头部裁剪
        cut = 0
        for index, (ts, _, _, _) in enumerate(self._prints):
            if ts >= floor:
                break
            cut = index + 1
        if cut:
            del self._prints[:cut]

    # ------------------------------------------------------------------ 输出

    def snapshot(self, *, at: Milliseconds) -> TradeFeatures:
        """窗口快照 → `TradeFeatures`（窗口为空 ⇒ None，不伪造成交量/价格）。"""
        self._prune(at=int(at))
        prints = self._prints
        if not prints:
            return TradeFeatures(trade_stream_available=self._total_count > 0,
                                 buy_aggressive_volume=None, sell_aggressive_volume=None,
                                 signed_volume=None, cvd=None, trade_count=None,
                                 trade_intensity=None, vwap=None)
        buy = math.fsum(qty for _, _, qty, is_buy in prints if is_buy)
        sell = math.fsum(qty for _, _, qty, is_buy in prints if not is_buy)
        volume = buy + sell
        notional = math.fsum(price * qty for _, price, qty, _ in prints)
        elapsed_ms = max(1, int(at) - prints[0][0])
        return TradeFeatures(
            trade_stream_available=True,
            buy_aggressive_volume=buy,
            sell_aggressive_volume=sell,
            signed_volume=buy - sell,
            # CVD = 窗口内主动买量累计 − 主动卖量累计（与 signed_volume 同源，命名遵循既有字段语义）
            cvd=buy - sell,
            trade_count=len(prints),
            # 每秒成交笔数（窗口内），低于 1s 的窗口按 1s 计以免除零
            trade_intensity=(len(prints) * 1000.0) / elapsed_ms,
            vwap=(notional / volume) if volume > 0 else None,
        )

    # ------------------------------------------------------------------ 只读

    @property
    def total_count(self) -> int:
        return self._total_count

    @property
    def total_volume(self) -> float:
        return self._total_volume

    @property
    def first_timestamp(self) -> Milliseconds | None:
        return self._first_ts


__all__ = ["TradeFeatureAccumulator"]
