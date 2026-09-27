"""Prediction provider 契约与实现。"""

from __future__ import annotations

from prediction.providers.base import PredictionProvider, ProviderResponse
from prediction.providers.jev import JevProvider, JevTransport
from prediction.providers.openrouter import (
    DEFAULT_TIMEOUT_S,
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_ENDPOINT,
    OPENROUTER_MODEL,
    OpenRouterCall,
    OpenRouterTransport,
    build_chat_completions_body,
    extract_message_content,
    map_http_status,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "JevProvider",
    "JevTransport",
    "OPENROUTER_API_KEY_ENV",
    "OPENROUTER_ENDPOINT",
    "OPENROUTER_MODEL",
    "OpenRouterCall",
    "OpenRouterTransport",
    "PredictionProvider",
    "ProviderResponse",
    "build_chat_completions_body",
    "extract_message_content",
    "map_http_status",
]
