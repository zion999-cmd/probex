"""调度层：资格闸门、并发上限、迟到结果判定。

这一层只做决策，不做 IO：runtime 负责调用 provider，调度层负责回答

- 这个 MarketState 该不该问 Jev？（eligibility gate）
- 现在还能不能发请求？（limited inflight，默认 1）
- 回来的结果能不能成为 current prediction？（stale response rejection，迟到旧结果只能被记录）

`check_eligibility` 与 P0001.3 的 `DataQuality.tradeable` 等价（healthy ∧ history_ready ∧
age_valid），只是额外给出原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from market.state.types import MarketState
from prediction.types import PredictionOutcome, PredictionRecord, PredictionRequest


class EligibilityReason(Enum):
    """不满足资格闸门的原因。"""

    ELIGIBLE = "eligible"
    BOOK_NOT_HEALTHY = "book_not_healthy"
    NOT_HISTORY_READY = "not_history_ready"
    AGE_INVALID = "age_invalid"


@dataclass(frozen=True, slots=True)
class Eligibility:
    eligible: bool
    reason: EligibilityReason


def check_eligibility(state: MarketState) -> Eligibility:
    """判断某个 MarketState 是否具备发起预测请求的资格。"""
    quality = state.quality
    if not quality.book_healthy:
        return Eligibility(False, EligibilityReason.BOOK_NOT_HEALTHY)
    if not quality.history_ready:
        return Eligibility(False, EligibilityReason.NOT_HISTORY_READY)
    if not quality.age_valid:
        return Eligibility(False, EligibilityReason.AGE_INVALID)
    return Eligibility(True, EligibilityReason.ELIGIBLE)


class PredictionScheduler:
    """请求准入与结果准入的唯一 Owner。"""

    def __init__(self, *, max_inflight: int = 1) -> None:
        if max_inflight <= 0:
            raise ValueError("max_inflight must be > 0")
        self._max_inflight = max_inflight
        self._inflight = 0
        self._last_sequence = -1
        self._latest: PredictionRecord | None = None
        self._latest_sequence = -1
        self._accepted = 0
        self._stale = 0

    @property
    def max_inflight(self) -> int:
        return self._max_inflight

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def latest_accepted(self) -> PredictionRecord | None:
        """当前有效的预测记录（迟到旧结果永远不会成为它）。"""
        return self._latest

    @property
    def latest_sequence(self) -> int:
        return self._latest_sequence

    @property
    def accepted_count(self) -> int:
        return self._accepted

    @property
    def stale_count(self) -> int:
        return self._stale

    def check(self, state: MarketState) -> Eligibility:
        return check_eligibility(state)

    def reserve(self) -> int | None:
        """占用一个 inflight 名额并返回请求序号；名额已满返回 `None`。

        不做请求排队：实时流里积压的旧请求没有价值。
        """
        if self._inflight >= self._max_inflight:
            return None
        self._inflight += 1
        self._last_sequence += 1
        return self._last_sequence

    def accept(self, *, request: PredictionRequest, record: PredictionRecord) -> PredictionOutcome:
        """交付一个已完成请求的结果，返回 `ACCEPTED` 或 `STALE_RESPONSE`。"""
        if self._inflight <= 0:
            raise RuntimeError("no inflight request to complete")
        self._inflight -= 1
        if request.sequence < self._latest_sequence:
            self._stale += 1
            return PredictionOutcome.STALE_RESPONSE
        self._latest = record
        self._latest_sequence = request.sequence
        self._accepted += 1
        return PredictionOutcome.ACCEPTED

    def abandon(self) -> None:
        """释放 inflight 名额但不产生记录（超时 / 失败路径）。"""
        if self._inflight <= 0:
            raise RuntimeError("no inflight request to abandon")
        self._inflight -= 1

    def record_replayed(self, record: PredictionRecord) -> None:
        """RECORDED 模式下把查到的历史记录设为 current。

        查找不是竞态，因此不做 staleness 判定。
        """
        self._latest = record
        self._latest_sequence = max(self._latest_sequence, record.sequence)
