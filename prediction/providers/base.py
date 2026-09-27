"""PredictionProvider 契约。

provider 的职责边界：把 `PredictionRequest` 变成原始响应文本 + 元数据。
**不做**解析、不做 TTL 判断、不做并发控制、不做 staleness 判断——那些属于 runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from prediction.types import PredictionRequest


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """provider 的原始返回。`raw_response` 原样落进 `PredictionRecord` 作为证据。"""

    provider: str
    model: str
    raw_response: str


@runtime_checkable
class PredictionProvider(Protocol):
    """预测 provider。实现必须是 async，且不得阻塞调用方事件循环。"""

    async def predict(self, request: PredictionRequest) -> ProviderResponse: ...
