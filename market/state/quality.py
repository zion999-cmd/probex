"""DataQuality：市场数据可用性的正式闸门。

作用范围仅限「市场数据是否可用于决策」。资金、仓位、Risk 不属于本层
（P0001.3 §10）。

约定：

- `history_ready`、`feature_ready`、`age_valid`、`tradeable` 都在这里唯一地派生，
  不允许下游各自重写这套判断。
- 未知 ≠ 0：年龄 / 覆盖度不足时宁可标记不可用，也不静默当成 0。
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import Milliseconds
from market.health.state import BookHealth


@dataclass(frozen=True, slots=True)
class DataQuality:
    """市场数据质量与可用性。"""

    #: 盘口可信度原始状态。
    book_health: BookHealth
    #: 距最近一次成功应用盘口更新的时间；从未应用过则为 None。
    book_age_ms: Milliseconds | None
    trade_stream_available: bool
    trade_age_ms: Milliseconds | None
    #: 粘性标志：本引擎生命周期内是否从未出现 sequence gap。
    sequence_contiguous: bool
    #: 价格历史是否有足够覆盖度支撑最长的 return 窗口。
    history_ready: bool
    window_coverage_ms: Milliseconds
    #: book 相关 feature 段的可用比例，[0, 1]。
    completeness: float
    feature_ready: bool
    age_valid: bool
    #: 市场数据是否可用于决策。不包含资金 / 仓位 / Risk。
    tradeable: bool

    @property
    def book_healthy(self) -> bool:
        """盘口是否处于 `HEALTHY`。"""
        return self.book_health is BookHealth.HEALTHY


def build_data_quality(
    *,
    book_health: BookHealth,
    book_age_ms: Milliseconds | None,
    sequence_contiguous: bool,
    window_coverage_ms: Milliseconds,
    history_window_ms: Milliseconds,
    completeness: float,
    trade_stream_available: bool = False,
    trade_age_ms: Milliseconds | None = None,
    max_book_age_ms: Milliseconds | None = None,
) -> DataQuality:
    """派生全部质量标志。

    `max_book_age_ms` 为 None 表示本阶段不做年龄阈值门控（只要求年龄已知）。
    """
    book_healthy = book_health is BookHealth.HEALTHY
    history_ready = window_coverage_ms >= history_window_ms
    feature_ready = book_healthy and history_ready
    age_valid = book_age_ms is not None and (max_book_age_ms is None or book_age_ms <= max_book_age_ms)
    return DataQuality(
        book_health=book_health,
        book_age_ms=book_age_ms,
        trade_stream_available=trade_stream_available,
        trade_age_ms=trade_age_ms,
        sequence_contiguous=sequence_contiguous,
        history_ready=history_ready,
        window_coverage_ms=window_coverage_ms,
        completeness=completeness,
        feature_ready=feature_ready,
        age_valid=age_valid,
        tradeable=feature_ready and age_valid,
    )
