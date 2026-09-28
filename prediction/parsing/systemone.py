"""Typed Jev answers → `jev-market-v1` domain answers。

System One 的 wire 契约（已核实：`docs.typesafe.ai/api` 与 P0001.4.1 的 P2 实测）：

```jsonc
{ "id": "gen-dec-...", "model": "typesafe/jev-1.13-<date>", "provider": "TypeSafe",
  "answers": {
     "<choice qid>": { "type": "choice", "choice": "<opt>", "probabilities": {"<opt>": p, ...}, "confidence": 0..1 },
     "<noul qid>":   { "type": "noul", "noul": 0..1 }   // Noul 没有 confidence
  },
  "usage": { "input_tokens": int, "output_tokens": int, "cost": float } }
```

职责：**只做 wire → domain 的确定性映射**，不猜、不修值、不丢概率：

- Choice 的 `probabilities` 直接成为 `FutureReturnDistribution`（五分类、求和必须为 1，否则 fail closed）。
- Noul 的 `noul` 直接成为二元概率（[0,1]）。
- Noul 永不产生 confidence；`provider_confidence` 只取最近 horizon Choice 的 `confidence`（缺失则为 `None`）。
- 响应外层与 answers 内的未知字段一律忽略，不进入 domain answers（避免污染上层 Prediction）。
- 产物是 `jev-market-v1` 形状的 JSON，交由**既有** `parse_jev_prediction` 生成 `Prediction`。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

from prediction.errors import PredictionInvalidResponseError, PredictionParseError
from prediction.schema.market_v1 import (
    PROBABILITY_QUESTIONS,
    PROBABILITY_SUM_TOLERANCE,
    QUESTION_SCHEMA_VERSION,
    horizon_key,
)
from prediction.systemone_wire import (
    ANSWER_TYPE_FIELD,
    BINARY_QUESTION_WIRE_IDS,
    CHOICE_FIELD,
    CONFIDENCE_FIELD,
    NOUL_FIELD,
    PROBABILITIES_FIELD,
    future_return_question_id,
)
from prediction.types import FUTURE_RETURN_CATEGORIES, ProviderUsage

#: answers 里可接受的 type 取值（Score 不在本阶段契约内）。
_ANSWER_TYPES = frozenset({"choice", "noul"})


@dataclass(frozen=True, slots=True)
class SystemOneAnswers:
    """解析结果：domain answers + provider 侧证据。"""

    domain_answers: dict[str, object]
    resolved_model: str | None
    provider: str | None
    response_id: str | None
    usage: ProviderUsage | None


def parse_systemone_response(
    raw_response: str,
    *,
    horizons_ms: tuple[int, ...],
    question_schema_version: str = QUESTION_SCHEMA_VERSION,
) -> SystemOneAnswers:
    """解析 System One 响应并映射为 domain answers。非法响应抛 `PredictionError` 子类。"""
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise PredictionInvalidResponseError("System One response body is empty")
    try:
        envelope = json.loads(raw_response, parse_constant=_reject_constant, object_pairs_hook=_pairs_to_object)
    except (PredictionParseError, PredictionInvalidResponseError):
        raise
    except json.JSONDecodeError as exc:
        raise PredictionParseError(f"System One response is not valid JSON: {exc}") from exc
    if not isinstance(envelope, Mapping):
        raise PredictionParseError(f"System One response must be a JSON object, got {type(envelope).__name__}")

    answers = envelope.get("answers")
    if not isinstance(answers, Mapping):
        raise PredictionInvalidResponseError("System One response has no 'answers' object")

    future_return = _parse_future_return(answers, horizons_ms=horizons_ms)
    binary = {
        question: _parse_noul(answers, BINARY_QUESTION_WIRE_IDS[question]) for question in PROBABILITY_QUESTIONS
    }

    domain_answers: dict[str, object] = {
        "question_schema_version": question_schema_version,
        "future_return": future_return,
        **binary,
    }
    nearest_confidence = _nearest_horizon_confidence(answers, horizons_ms=horizons_ms)
    if nearest_confidence is not None:
        domain_answers["confidence"] = nearest_confidence

    return SystemOneAnswers(
        domain_answers=domain_answers,
        resolved_model=_optional_str(envelope.get("model")),
        provider=_optional_str(envelope.get("provider")),
        response_id=_optional_str(envelope.get("id")),
        usage=_parse_usage(envelope.get("usage")),
    )


def _parse_future_return(answers: Mapping[str, object], *, horizons_ms: tuple[int, ...]) -> dict[str, object]:
    distribution: dict[str, object] = {}
    for horizon in horizons_ms:
        question_id = _horizon_question_id(horizon)
        entry = _require_answer(answers, question_id)
        answer_type = _require_answer_type(entry, question_id)
        if answer_type != "choice":
            raise PredictionInvalidResponseError(
                f"answers[{question_id!r}].type must be 'choice' for a future-return question, got {answer_type!r}"
            )
        probabilities = entry.get(PROBABILITIES_FIELD)
        if not isinstance(probabilities, Mapping):
            raise PredictionInvalidResponseError(f"answers[{question_id!r}] has no 'probabilities' object")

        buckets: dict[str, float] = {}
        for category in FUTURE_RETURN_CATEGORIES:
            if category not in probabilities:
                raise PredictionInvalidResponseError(
                    f"answers[{question_id!r}].probabilities is missing category {category!r}"
                )
            buckets[category] = _require_probability(probabilities[category], f"answers[{question_id!r}].probabilities.{category}")

        total = math.fsum(buckets.values())
        if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
            raise PredictionInvalidResponseError(
                f"answers[{question_id!r}].probabilities sum to {total} (tolerance {PROBABILITY_SUM_TOLERANCE})"
            )

        declared_choice = entry.get(CHOICE_FIELD)
        if declared_choice is not None and declared_choice not in FUTURE_RETURN_CATEGORIES:
            raise PredictionInvalidResponseError(
                f"answers[{question_id!r}].choice {declared_choice!r} is not one of the five categories"
            )
        distribution[horizon_key(horizon)] = buckets
    return distribution


def _parse_noul(answers: Mapping[str, object], question_id: str) -> float:
    entry = _require_answer(answers, question_id)
    answer_type = _require_answer_type(entry, question_id)
    if answer_type != "noul":
        raise PredictionInvalidResponseError(
            f"answers[{question_id!r}].type must be 'noul', got {answer_type!r}"
        )
    return _require_probability(entry.get(NOUL_FIELD), f"answers[{question_id!r}].noul")


def _nearest_horizon_confidence(answers: Mapping[str, object], *, horizons_ms: tuple[int, ...]) -> float | None:
    """`provider_confidence` 只来自最近 horizon 的 Choice `confidence`；缺失则为 None（不伪造）。"""
    nearest = min(horizons_ms)
    entry = answers.get(_horizon_question_id(nearest))
    if not isinstance(entry, Mapping):
        return None
    confidence = entry.get(CONFIDENCE_FIELD)
    if confidence is None:
        return None
    return _require_probability(confidence, f"answers[{_horizon_question_id(nearest)!r}].confidence")


def _horizon_question_id(horizon_ms: int) -> str:
    """wire question id（由 `prediction/systemone_wire.py` 统一命名）。"""
    return future_return_question_id(horizon_ms)


def _require_answer(answers: Mapping[str, object], question_id: str) -> Mapping[str, object]:
    entry = answers.get(question_id)
    if not isinstance(entry, Mapping):
        raise PredictionInvalidResponseError(f"answers is missing question {question_id!r}")
    return entry


def _require_answer_type(entry: Mapping[str, object], question_id: str) -> str:
    answer_type = entry.get(ANSWER_TYPE_FIELD)
    if not isinstance(answer_type, str) or answer_type not in _ANSWER_TYPES:
        raise PredictionInvalidResponseError(
            f"answers[{question_id!r}].type must be one of {sorted(_ANSWER_TYPES)}, got {answer_type!r}"
        )
    return answer_type


def _require_probability(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PredictionInvalidResponseError(f"{path} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise PredictionInvalidResponseError(f"{path} must be finite, got {value!r}")
    if not 0.0 <= number <= 1.0:
        raise PredictionInvalidResponseError(f"{path} must be in [0, 1], got {number!r}")
    return number


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_usage(value: object) -> ProviderUsage | None:
    if not isinstance(value, Mapping):
        return None
    return ProviderUsage(
        input_tokens=_optional_int(value.get("input_tokens")),
        output_tokens=_optional_int(value.get("output_tokens")),
        cost=_optional_float(value.get("cost")),
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _reject_constant(name: str) -> object:
    raise PredictionInvalidResponseError(f"System One response contains non-finite JSON constant {name!r}")


def _pairs_to_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PredictionInvalidResponseError(f"System One response contains duplicate key {key!r}")
        result[key] = value
    return result


__all__ = ["SystemOneAnswers", "parse_systemone_response"]
