"""严格 parser：Jev 的结构化概率必须完全符合 question schema。

原则（P0001.4 §10）：

- `0 <= p <= 1`；NaN / Infinity / 负数 / 超界一律 `INVALID_RESPONSE`。
- 互斥分类必须齐备、不重复，并在容差内求和为 1。
- 未知字段、缺字段、重复键、类型错误一律 fail closed。
- 绝不「猜着修」：失败就抛错，由 runtime 记为失败类型，不生成伪 Prediction。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

from prediction.errors import PredictionInvalidResponseError, PredictionParseError
from prediction.schema.market_v1 import (
    FUTURE_RETURN_HORIZONS_MS,
    PROBABILITY_QUESTIONS,
    PROBABILITY_SUM_TOLERANCE,
    QUESTION_SCHEMA_VERSION,
    horizon_key,
)
from prediction.types import FUTURE_RETURN_CATEGORIES, FutureReturnDistribution, Prediction

#: 响应顶层允许的键（其余一律视为未知字段）。
_RESPONSE_KEYS: frozenset[str] = frozenset(
    {"question_schema_version", "future_return", *PROBABILITY_QUESTIONS, "confidence"}
)

#: 均匀分布下的最大熵，用于派生置信度。
_MAX_ENTROPY = math.log(len(FUTURE_RETURN_CATEGORIES))


def parse_jev_prediction(
    raw_response: str,
    *,
    expected_question_schema_version: str = QUESTION_SCHEMA_VERSION,
) -> Prediction:
    """把原始响应文本解析为 `Prediction`。非法响应抛 `PredictionError` 子类。"""
    message = _load_object(raw_response)
    _reject_unknown_keys(message, allowed=_RESPONSE_KEYS, path="response")

    version = _require_str(message, "question_schema_version", path="response")
    if version != expected_question_schema_version:
        raise PredictionInvalidResponseError(
            f"response.question_schema_version {version!r} does not match expected "
            f"{expected_question_schema_version!r}"
        )

    distributions = _parse_future_return(message.get("future_return"))
    probabilities = {
        key: _require_probability(message, key, path="response") for key in PROBABILITY_QUESTIONS
    }
    provider_confidence = _optional_probability(message, "confidence", path="response")

    return Prediction(
        future_return=distributions,
        buy_adverse_selection=probabilities["buy_adverse_selection"],
        sell_adverse_selection=probabilities["sell_adverse_selection"],
        buy_fill_probability=probabilities["buy_fill_probability"],
        sell_fill_probability=probabilities["sell_fill_probability"],
        provider_confidence=provider_confidence,
        derived_confidence=derived_confidence(distributions[0]),
    )


def derived_confidence(distribution: FutureReturnDistribution) -> float:
    """由分布熵派生的置信度：`1 - H / ln(5)`，值域 [0, 1]。

    浮点误差可能让它极其轻微地越出 [0, 1]（例如均匀分布时得到 -2.2e-16），
    因此这里做数值夹取。
    """
    value = 1.0 - distribution.entropy / _MAX_ENTROPY
    return max(0.0, min(1.0, value))


def _parse_future_return(raw: object) -> tuple[FutureReturnDistribution, ...]:
    if raw is None:
        raise PredictionInvalidResponseError("response: missing required field 'future_return'")
    message = _require_mapping(raw, path="response.future_return")
    expected: dict[str, int] = {horizon_key(horizon): horizon for horizon in FUTURE_RETURN_HORIZONS_MS}
    _reject_unknown_keys(message, allowed=frozenset(expected), path="response.future_return")

    missing = sorted(key for key in expected if key not in message)
    if missing:
        raise PredictionInvalidResponseError(f"response.future_return: missing horizons {missing}")

    distributions: list[FutureReturnDistribution] = []
    for key, horizon_ms in expected.items():
        path = f"response.future_return.{key}"
        entry = _require_mapping(message[key], path=path)
        _reject_unknown_keys(entry, allowed=frozenset(FUTURE_RETURN_CATEGORIES), path=path)

        missing_categories = [category for category in FUTURE_RETURN_CATEGORIES if category not in entry]
        if missing_categories:
            raise PredictionInvalidResponseError(f"{path}: missing categories {missing_categories}")

        values = {
            category: _require_probability(entry, category, path=path) for category in FUTURE_RETURN_CATEGORIES
        }
        distribution = FutureReturnDistribution(horizon_ms=horizon_ms, **values)
        if abs(distribution.probability_sum - 1.0) > PROBABILITY_SUM_TOLERANCE:
            raise PredictionInvalidResponseError(
                f"{path}: probability sum {distribution.probability_sum} does not equal 1 "
                f"within tolerance {PROBABILITY_SUM_TOLERANCE}"
            )
        distributions.append(distribution)
    return tuple(distributions)


def _load_object(raw_response: object) -> Mapping[str, object]:
    if not isinstance(raw_response, str) or not raw_response.strip():
        # 空响应属于 provider 契约违反，不是 JSON 语法错误，因此归为 INVALID_RESPONSE
        raise PredictionInvalidResponseError("raw response must be a non-empty string")
    try:
        parsed = json.loads(raw_response, parse_constant=_reject_constant, object_pairs_hook=_pairs_to_object)
    except (PredictionParseError, PredictionInvalidResponseError):
        raise
    except json.JSONDecodeError as exc:
        raise PredictionParseError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise PredictionParseError(f"response must be a JSON object, got {type(parsed).__name__}")
    return parsed


def _reject_constant(name: str) -> object:
    raise PredictionInvalidResponseError(f"response contains non-finite JSON constant {name!r}")


def _pairs_to_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PredictionInvalidResponseError(f"response contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_unknown_keys(message: Mapping[str, object], *, allowed: frozenset[str], path: str) -> None:
    unknown = sorted(key for key in message if key not in allowed)
    if unknown:
        raise PredictionInvalidResponseError(f"{path}: unknown fields {unknown}")


def _require_mapping(value: object, *, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PredictionInvalidResponseError(f"{path}: expected an object, got {type(value).__name__}")
    return value


def _require_str(message: Mapping[str, object], key: str, *, path: str) -> str:
    if key not in message:
        raise PredictionInvalidResponseError(f"{path}: missing required field {key!r}")
    value = message[key]
    if not isinstance(value, str) or not value:
        raise PredictionInvalidResponseError(f"{path}.{key}: expected a non-empty string")
    return value


def _require_probability(message: Mapping[str, object], key: str, *, path: str) -> float:
    if key not in message:
        raise PredictionInvalidResponseError(f"{path}: missing required field {key!r}")
    value = message[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PredictionInvalidResponseError(f"{path}.{key}: expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise PredictionInvalidResponseError(f"{path}.{key}: expected a finite probability, got {value!r}")
    if not 0.0 <= number <= 1.0:
        raise PredictionInvalidResponseError(f"{path}.{key}: probability must be in [0, 1], got {number!r}")
    return number


def _optional_probability(message: Mapping[str, object], key: str, *, path: str) -> float | None:
    if key not in message:
        return None
    return _require_probability(message, key, path=path)
