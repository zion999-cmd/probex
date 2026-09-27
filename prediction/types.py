"""Prediction Runtime 的核心类型。

不可变约定：

- `PredictionRequest` 在创建时冻结输入（payload 以 canonical JSON 字符串保存），
  之后市场继续变化也不会改变这个请求。
- `Prediction` / `PredictionRecord` 一旦生成不允许修改；判断错了只能新增
  outcome / evaluation（本阶段不实现），不能回头改 Prediction。

时间约定：所有时间戳来自运行时的 `Clock`，本包不使用 wall-clock。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from market.events.types import Milliseconds, Venue

#: `future_return` 分布的分类顺序（与 question schema 一致）。
FUTURE_RETURN_CATEGORIES: tuple[str, ...] = ("strong_down", "down", "flat", "up", "strong_up")


@runtime_checkable
class Clock(Protocol):
    """毫秒时间源。必须单调不减。`market.replay.clock.ReplayClock` 结构上即满足。"""

    def now(self) -> Milliseconds: ...


class PredictionMode(Enum):
    """研究语义：历史记录复现 vs 今日重新问模型。两者不得混在同一实验里。"""

    #: 严格复现：只查历史 PredictionRecord，不调用 provider。
    RECORDED = "recorded"
    #: 新实验：用今天的模型重新调用。
    LIVE_REQUERY = "live_requery"


class ProviderStatus(Enum):
    """provider 健康状态（由连续失败次数与 next_retry_at 派生）。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BACKING_OFF = "backing_off"


class PredictionOutcome(Enum):
    """一次 submit 的结果分类。"""

    ACCEPTED = "accepted"
    #: 迟到的旧结果：记录，但不能成为 current prediction。
    STALE_RESPONSE = "stale_response"
    SKIPPED_INFLIGHT = "skipped_inflight"
    SKIPPED_BACKOFF = "skipped_backoff"
    NOT_ELIGIBLE = "not_eligible"
    #: RECORDED 模式下没有对应历史记录。
    NOT_RECORDED = "not_recorded"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    PROVIDER_ERROR = "provider_error"
    PARSE_ERROR = "parse_error"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True, slots=True)
class PredictionRequest:
    """冻结的预测请求。"""

    sequence: int
    request_id: str
    venue: Venue
    symbol: str
    as_of: Milliseconds
    market_state_hash: str
    feature_schema_version: str
    question_schema_version: str
    horizons_ms: tuple[Milliseconds, ...]
    created_at: Milliseconds
    expires_at: Milliseconds
    #: canonical Jev payload（canonical JSON 字符串，不可再变）。
    payload_json: str

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("sequence must be >= 0")
        for field in ("request_id", "market_state_hash", "payload_json"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field} must be a non-empty string")
        if not self.horizons_ms:
            raise ValueError("horizons_ms must not be empty")
        if self.expires_at < self.created_at:
            raise ValueError("expires_at must be >= created_at")


@dataclass(frozen=True, slots=True)
class FutureReturnDistribution:
    """单个 horizon 的未来收益分布。分类集合由 question schema 固定。"""

    horizon_ms: Milliseconds
    strong_down: float
    down: float
    flat: float
    up: float
    strong_up: float

    def __post_init__(self) -> None:
        for category in FUTURE_RETURN_CATEGORIES:
            probability = getattr(self, category)
            if not _is_probability(probability):
                raise ValueError(f"future_return.{category} must be a probability in [0, 1], got {probability!r}")

    def as_mapping(self) -> tuple[tuple[str, float], ...]:
        """(category, probability) 有序元组，便于比较与审计。"""
        return tuple((category, getattr(self, category)) for category in FUTURE_RETURN_CATEGORIES)

    @property
    def probability_sum(self) -> float:
        return math.fsum(getattr(self, category) for category in FUTURE_RETURN_CATEGORIES)

    @property
    def entropy(self) -> float:
        """自然对数熵。"""
        return -math.fsum(
            probability * math.log(probability) for _, probability in self.as_mapping() if probability > 0.0
        )


@dataclass(frozen=True, slots=True)
class Prediction:
    """结构化概率输出。绝不包含 BUY / SELL / HOLD 之类的动作。"""

    future_return: tuple[FutureReturnDistribution, ...]
    buy_adverse_selection: float
    sell_adverse_selection: float
    buy_fill_probability: float
    sell_fill_probability: float
    #: 供应商给出的置信度；缺失时为 None。
    provider_confidence: float | None
    #: 由分布派生的置信度，来源与 provider_confidence 可区分。
    derived_confidence: float

    def __post_init__(self) -> None:
        if not self.future_return:
            raise ValueError("future_return must not be empty")
        horizons = [distribution.horizon_ms for distribution in self.future_return]
        if len(set(horizons)) != len(horizons):
            raise ValueError("future_return contains duplicate horizons")
        if horizons != sorted(horizons):
            raise ValueError("future_return must be ordered by horizon_ms")
        for field in (
            "buy_adverse_selection",
            "sell_adverse_selection",
            "buy_fill_probability",
            "sell_fill_probability",
            "derived_confidence",
        ):
            value = getattr(self, field)
            if not _is_probability(value):
                raise ValueError(f"Prediction.{field} must be a probability in [0, 1], got {value!r}")
        if self.provider_confidence is not None and not _is_probability(self.provider_confidence):
            raise ValueError(f"Prediction.provider_confidence must be a probability in [0, 1], got {self.provider_confidence!r}")

    def distribution(self, horizon_ms: Milliseconds) -> FutureReturnDistribution | None:
        for distribution in self.future_return:
            if distribution.horizon_ms == horizon_ms:
                return distribution
        return None


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    """一次成功预测的完整证据。生成后不可修改。"""

    request_id: str
    sequence: int
    market_state_hash: str
    feature_schema_version: str
    question_schema_version: str
    provider: str
    model: str
    mode: PredictionMode
    as_of: Milliseconds
    request_created_at: Milliseconds
    response_received_at: Milliseconds
    latency_ms: Milliseconds
    expires_at: Milliseconds
    raw_response: str
    prediction: Prediction

    def is_expired(self, at: Milliseconds) -> bool:
        """`at` 时刻该预测是否已过期（`at > expires_at`）。"""
        return at > self.expires_at

    def ttl_remaining_ms(self, at: Milliseconds) -> Milliseconds:
        """剩余寿命，已过期时为 0。"""
        return max(0, self.expires_at - at)


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """一次 `submit` 的结果。失败时 `record` 必为 None。"""

    outcome: PredictionOutcome
    request: PredictionRequest | None
    record: PredictionRecord | None
    error: PredictionError | None
    completed_at: Milliseconds

    @property
    def accepted(self) -> bool:
        return self.outcome is PredictionOutcome.ACCEPTED


class PredictionArchive(Protocol):
    """历史 PredictionRecord 的只读/写入接口（本阶段只有内存实现）。"""

    def find(self, *, market_state_hash: str, question_schema_version: str) -> PredictionRecord | None: ...

    def store(self, record: PredictionRecord) -> None: ...


class InMemoryPredictionArchive:
    """内存 archive：保存被接受的记录，用于 RECORDED 复现。

    持久化（Parquet / 事务库）不属于 P0001.4 范围。
    同一个 `market_state_hash` 可能对应多条记录（例如 LIVE_REQUERY 用不同模型
    重新问过同一个状态）：全部保留，但 `find` 返回最早的一条，即该状态首次被
    预测时的原始证据。
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], list[PredictionRecord]] = {}

    def find(self, *, market_state_hash: str, question_schema_version: str) -> PredictionRecord | None:
        records = self._records.get((market_state_hash, question_schema_version))
        return records[0] if records else None

    def store(self, record: PredictionRecord) -> None:
        key = (record.market_state_hash, record.question_schema_version)
        self._records.setdefault(key, []).append(record)

    def __len__(self) -> int:
        """已记录的不同 MarketState 数量。"""
        return len(self._records)

    @property
    def records(self) -> tuple[PredictionRecord, ...]:
        """全部记录，按首次写入顺序。"""
        return tuple(record for records in self._records.values() for record in records)


def _is_probability(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and 0.0 <= number <= 1.0
