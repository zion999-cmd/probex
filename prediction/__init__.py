"""Jev Prediction Runtime：MarketState → 可审计的 PredictionRecord。

本包只做预测运行时：不交易、不做 Strategy、不做仓位决策，也不让 Jev 看到账户信息。
"""

from __future__ import annotations

from prediction.errors import (
    PredictionError,
    PredictionInvalidResponseError,
    PredictionParseError,
    PredictionProviderError,
    PredictionTimeoutError,
    PredictionTransportError,
)
from prediction.parsing.market_v1 import derived_confidence, parse_jev_prediction
from prediction.providers.base import PredictionProvider, ProviderResponse
from prediction.providers.jev import JevProvider, JevTransport
from prediction.providers.openrouter import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_ENDPOINT,
    OPENROUTER_MODEL,
    OpenRouterCall,
    OpenRouterTransport,
)
from prediction.runtime import PredictionRuntime, outcome_for_error
from prediction.scheduler import Eligibility, EligibilityReason, PredictionScheduler, check_eligibility
from prediction.schema.market_v1 import (
    FUTURE_RETURN_HORIZONS_MS,
    PROBABILITY_QUESTIONS,
    QUESTION_SCHEMA_VERSION,
    QUESTION_SPECS,
    QuestionSpec,
    build_jev_payload,
    build_jev_payload_json,
    build_prediction_request,
    market_state_hash,
)
from prediction.types import (
    FUTURE_RETURN_CATEGORIES,
    Clock,
    FutureReturnDistribution,
    InMemoryPredictionArchive,
    Prediction,
    PredictionArchive,
    PredictionMode,
    PredictionOutcome,
    PredictionRecord,
    PredictionRequest,
    PredictionResult,
    ProviderStatus,
)

__all__ = [
    "FUTURE_RETURN_CATEGORIES",
    "FUTURE_RETURN_HORIZONS_MS",
    "OPENROUTER_API_KEY_ENV",
    "OPENROUTER_ENDPOINT",
    "OPENROUTER_MODEL",
    "PROBABILITY_QUESTIONS",
    "QUESTION_SCHEMA_VERSION",
    "QUESTION_SPECS",
    "Clock",
    "Eligibility",
    "EligibilityReason",
    "FutureReturnDistribution",
    "InMemoryPredictionArchive",
    "JevProvider",
    "JevTransport",
    "OpenRouterCall",
    "OpenRouterTransport",
    "Prediction",
    "PredictionArchive",
    "PredictionError",
    "PredictionInvalidResponseError",
    "PredictionMode",
    "PredictionOutcome",
    "PredictionParseError",
    "PredictionProvider",
    "PredictionProviderError",
    "PredictionRecord",
    "PredictionRequest",
    "PredictionResult",
    "PredictionRuntime",
    "PredictionScheduler",
    "PredictionTimeoutError",
    "PredictionTransportError",
    "ProviderResponse",
    "ProviderStatus",
    "QuestionSpec",
    "build_jev_payload",
    "build_jev_payload_json",
    "build_prediction_request",
    "check_eligibility",
    "derived_confidence",
    "market_state_hash",
    "outcome_for_error",
    "parse_jev_prediction",
]
