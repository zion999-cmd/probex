"""市场预测 question schema v1（`jev-market-v1`）。

这一层把「问 Jev 什么问题」变成正式契约，而不是隐性 prompt：

- 问题集合、horizon、bucket 全部写进 schema，并随请求一起下发给 provider。
- bucket / horizon / wording semantics / output meaning 任一变化都必须升级
  `question_schema_version`。
- payload 只由 immutable `MarketState` 的**市场字段**构造；账户、仓位、盈亏、
  风险等未来领域对象绝不出现在 payload 中。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum

from market.events.types import Milliseconds, Venue
from market.state.types import MarketState
from prediction.types import FUTURE_RETURN_CATEGORIES, PredictionRequest

#: question schema 版本。
QUESTION_SCHEMA_VERSION = "jev-market-v1"

#: `future_return` 的 horizon 集合。
FUTURE_RETURN_HORIZONS_MS: tuple[Milliseconds, ...] = (5_000, 15_000, 30_000, 60_000)

#: 单概率问题集合。
PROBABILITY_QUESTIONS: tuple[str, ...] = (
    "buy_adverse_selection",
    "sell_adverse_selection",
    "buy_fill_probability",
    "sell_fill_probability",
)

#: 互斥分类求和的容差。
PROBABILITY_SUM_TOLERANCE = 1e-6

#: payload 的顶层键白名单（结构上不允许出现其它字段）。
PAYLOAD_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "question_schema_version",
    "feature_schema_version",
    "market_state_hash",
    "identity",
    "as_of",
    "quality",
    "price",
    "depth",
    "flow",
    "trade",
    "returns",
    "volatility",
    "questions",
)

#: 明确禁止出现在 payload 中的字段名（账户 / 仓位 / 盈亏 / 风险 / 决策）。
FORBIDDEN_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "account",
        "available_balance",
        "balance",
        "capital",
        "decision",
        "equity",
        "exposure",
        "inventory",
        "leverage",
        "liquidation_distance",
        "margin",
        "open_orders",
        "order",
        "orders",
        "pending_orders",
        "pnl",
        "position",
        "positions",
        "realized_pnl",
        "risk",
        "risk_limit",
        "risk_limits",
        "signal",
        "target_position",
        "unrealized_pnl",
    }
)


def horizon_key(horizon_ms: Milliseconds) -> str:
    """horizon 的审计友好键名，例如 `5000 -> "5s"`。"""
    return f"{horizon_ms // 1_000}s"


def horizon_from_key(key: str) -> Milliseconds:
    """把 `"5s"` 形式的键还原为毫秒；非法键抛 `ValueError`。"""
    if not isinstance(key, str) or not key.endswith("s"):
        raise ValueError(f"invalid horizon key {key!r}")
    seconds = key[:-1]
    if not seconds.isdigit():
        raise ValueError(f"invalid horizon key {key!r}")
    return int(seconds) * 1_000


@dataclass(frozen=True, slots=True)
class QuestionSpec:
    """一个预测问题的正式定义。"""

    key: str
    kind: str
    horizons_ms: tuple[Milliseconds, ...] = ()
    categories: tuple[str, ...] = ()
    probability_sum: float | None = None

    def as_json(self) -> dict[str, object]:
        encoded: dict[str, object] = {"key": self.key, "kind": self.kind}
        if self.horizons_ms:
            encoded["horizons"] = [horizon_key(horizon) for horizon in self.horizons_ms]
        if self.categories:
            encoded["categories"] = list(self.categories)
        if self.probability_sum is not None:
            encoded["required_probability_sum"] = self.probability_sum
        return encoded


QUESTION_SPECS: tuple[QuestionSpec, ...] = (
    QuestionSpec(
        key="future_return",
        kind="future_return_distribution",
        horizons_ms=FUTURE_RETURN_HORIZONS_MS,
        categories=FUTURE_RETURN_CATEGORIES,
        probability_sum=1.0,
    ),
    *(QuestionSpec(key=key, kind="probability") for key in PROBABILITY_QUESTIONS),
)

QUESTION_SPEC_BY_KEY: Mapping[str, QuestionSpec] = {spec.key: spec for spec in QUESTION_SPECS}


def to_jsonable(value: object) -> object:
    """把 dataclass / Enum / 序列递归转换为可 JSON 化的结构（键排序由 canonical_json 负责）。"""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(value[key]) for key in sorted(value, key=str)}
    return value


def canonical_json(value: object) -> str:
    """canonical JSON：键排序、紧凑分隔符、禁止非有限数值。

    与 Event Store codec 使用同一套规则（两者由交叉测试保持一致）。
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_market_state(state: MarketState) -> str:
    """全量 MarketState 的 canonical 表示。"""
    return canonical_json(to_jsonable(state))


def market_state_hash(state: MarketState) -> str:
    """MarketState 的内容寻址指纹：Jev 那一刻到底看到了什么。"""
    digest = hashlib.sha256(canonical_market_state(state).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_jev_payload(
    state: MarketState,
    *,
    question_schema_version: str = QUESTION_SCHEMA_VERSION,
) -> dict[str, object]:
    """由 immutable MarketState 构造 Jev payload（只含市场信息）。"""
    if question_schema_version != QUESTION_SCHEMA_VERSION:
        raise ValueError(f"unsupported question schema version {question_schema_version!r}")
    return {
        "question_schema_version": question_schema_version,
        "feature_schema_version": state.feature_schema_version,
        "market_state_hash": market_state_hash(state),
        "identity": to_jsonable(state.identity),
        "as_of": to_jsonable(state.time),
        "quality": to_jsonable(state.quality),
        "price": to_jsonable(state.price),
        "depth": to_jsonable(state.depth),
        "flow": to_jsonable(state.flow),
        "trade": to_jsonable(state.trade),
        "returns": to_jsonable(state.returns),
        "volatility": to_jsonable(state.volatility),
        "questions": [spec.as_json() for spec in QUESTION_SPECS],
    }


def build_jev_payload_json(
    state: MarketState,
    *,
    question_schema_version: str = QUESTION_SCHEMA_VERSION,
) -> str:
    """canonical payload JSON（同一 state + 同一 schema 必然字节级相同）。"""
    return canonical_json(build_jev_payload(state, question_schema_version=question_schema_version))


def build_prediction_request(
    state: MarketState,
    *,
    sequence: int,
    created_at: Milliseconds,
    ttl_ms: Milliseconds,
    question_schema_version: str = QUESTION_SCHEMA_VERSION,
) -> PredictionRequest:
    """由 MarketState 冻结出一个 `PredictionRequest`。"""
    if ttl_ms <= 0:
        raise ValueError("ttl_ms must be > 0")
    return PredictionRequest(
        sequence=sequence,
        request_id=f"{state.identity.symbol}-{sequence:08d}",
        venue=state.identity.venue,
        symbol=state.identity.symbol,
        as_of=state.time.as_of_exchange_ts,
        market_state_hash=market_state_hash(state),
        feature_schema_version=state.feature_schema_version,
        question_schema_version=question_schema_version,
        horizons_ms=FUTURE_RETURN_HORIZONS_MS,
        created_at=created_at,
        expires_at=created_at + ttl_ms,
        payload_json=build_jev_payload_json(state, question_schema_version=question_schema_version),
    )


__all__ = [
    "FORBIDDEN_PAYLOAD_FIELDS",
    "FUTURE_RETURN_HORIZONS_MS",
    "PAYLOAD_TOP_LEVEL_KEYS",
    "PROBABILITY_QUESTIONS",
    "PROBABILITY_SUM_TOLERANCE",
    "QUESTION_SCHEMA_VERSION",
    "QUESTION_SPECS",
    "QUESTION_SPEC_BY_KEY",
    "QuestionSpec",
    "Venue",
    "build_jev_payload",
    "build_jev_payload_json",
    "build_prediction_request",
    "canonical_json",
    "canonical_market_state",
    "horizon_from_key",
    "horizon_key",
    "market_state_hash",
    "to_jsonable",
]
