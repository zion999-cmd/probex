"""事件域订单流（OFI）。

这是 P0001.3 相对 V3 的核心升级：不再用「每秒 snapshot 的 successive L1 diff」近似，
而是对每一次有效盘口 mutation 计算 OFI（Cont–Kukanov–Stoikov 形式），再滚动聚合。

边界：`OrderBook` 只描述 mutation（`BookMutation`），本模块负责解释 mutation。

```text
bid 侧贡献：最优买价上升 → +新数量；下降 → -旧数量；不变 → 新数量 - 旧数量
ask 侧贡献：最优卖价下降 → -新数量；上升 → +旧数量；不变 → 旧数量 - 新数量
event_ofi = 一条 delta 内全部 mutation 贡献之和
ofi_h     = 时间窗内 event_ofi 之和（窗口为空 → None）
normalized_ofi_h = ofi_h / (bid_size + ask_size)（以当前 L1 深度归一）
```
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from market.book.order_book import BookMutation, BookSide
from market.events.payloads import PriceLevel
from market.events.types import Milliseconds
from market.features.windows import TimeWindow
from market.state.types import FlowFeatures

#: 固定 OFI 窗口。
OFI_WINDOWS_MS: tuple[int, ...] = (1_000, 5_000, 15_000)


def _bid_contribution(before: PriceLevel | None, after: PriceLevel | None) -> float:
    if before is None:
        return after.size if after is not None else 0.0
    if after is None:
        return -before.size
    if after.price > before.price:
        return after.size
    if after.price < before.price:
        return -before.size
    return after.size - before.size


def _ask_contribution(before: PriceLevel | None, after: PriceLevel | None) -> float:
    if before is None:
        return -after.size if after is not None else 0.0
    if after is None:
        return before.size
    if after.price < before.price:
        return -after.size
    if after.price > before.price:
        return before.size
    return before.size - after.size


def mutation_ofi(mutation: BookMutation) -> float:
    """单次盘口档位变化的 OFI 贡献。"""
    return _bid_contribution(mutation.best_bid_before, mutation.best_bid_after) + _ask_contribution(
        mutation.best_ask_before, mutation.best_ask_after
    )


class FlowFeaturesCalculator:
    """累计订单流状态：计数 + 滚动 OFI 窗口。

    计数为累计事实，不受盘口健康状态影响；窗口在盘口失健康时被清空，
    以免旧值冒充新数据。
    """

    def __init__(self, *, windows_ms: tuple[int, ...] = OFI_WINDOWS_MS) -> None:
        if not windows_ms:
            raise ValueError("windows_ms must not be empty")
        self._windows: dict[int, TimeWindow] = {window: TimeWindow(window) for window in windows_ms}
        self._last_event_ofi: float | None = None
        self._book_update_count = 0
        self._bid_update_count = 0
        self._ask_update_count = 0
        self._level_additions = 0
        self._level_removals = 0

    def observe(self, mutations: Sequence[BookMutation], *, timestamp: Milliseconds) -> float | None:
        """记录一条 delta 的全部 mutation，返回该事件的 OFI；无 mutation 时为 `None`。"""
        if not mutations:
            return None

        event_ofi = math.fsum(mutation_ofi(mutation) for mutation in mutations)
        for mutation in mutations:
            self._book_update_count += 1
            if mutation.side is BookSide.BID:
                self._bid_update_count += 1
            else:
                self._ask_update_count += 1
            if mutation.old_size == 0.0:
                self._level_additions += 1
            if mutation.new_size == 0.0:
                self._level_removals += 1
        for window in self._windows.values():
            window.observe(event_ofi, timestamp)

        self._last_event_ofi = event_ofi
        return event_ofi

    def invalidate_windows(self) -> None:
        """盘口不可用时清空滚动窗口。"""
        for window in self._windows.values():
            window.reset()
        self._last_event_ofi = None

    def snapshot(
        self,
        *,
        at: Milliseconds,
        best_bid_size: float | None,
        best_ask_size: float | None,
    ) -> FlowFeatures:
        """生成订单流 feature 段。"""
        denominator: float | None = None
        if best_bid_size is not None and best_ask_size is not None:
            total = best_bid_size + best_ask_size
            denominator = total if total != 0.0 else None

        def rolling(window_ms: int) -> float | None:
            window = self._windows.get(window_ms)
            return None if window is None else window.sum(at)

        def normalized(window_ms: int) -> float | None:
            value = rolling(window_ms)
            if value is None or denominator is None:
                return None
            return value / denominator

        return FlowFeatures(
            event_ofi=self._last_event_ofi,
            ofi_1s=rolling(1_000),
            ofi_5s=rolling(5_000),
            ofi_15s=rolling(15_000),
            normalized_ofi_1s=normalized(1_000),
            normalized_ofi_5s=normalized(5_000),
            normalized_ofi_15s=normalized(15_000),
            book_update_count=self._book_update_count,
            bid_update_count=self._bid_update_count,
            ask_update_count=self._ask_update_count,
            level_additions=self._level_additions,
            level_removals=self._level_removals,
        )
