"""预测 question schema。"""

from __future__ import annotations

from prediction.schema.market_v1 import (
    FORBIDDEN_PAYLOAD_FIELDS,
    FUTURE_RETURN_HORIZONS_MS,
    PAYLOAD_TOP_LEVEL_KEYS,
    PROBABILITY_QUESTIONS,
    PROBABILITY_SUM_TOLERANCE,
    QUESTION_SCHEMA_VERSION,
    QUESTION_SPECS,
    QUESTION_SPEC_BY_KEY,
    QuestionSpec,
    build_jev_payload,
    build_jev_payload_json,
    build_prediction_request,
    canonical_json,
    canonical_market_state,
    horizon_from_key,
    horizon_key,
    market_state_hash,
    to_jsonable,
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
