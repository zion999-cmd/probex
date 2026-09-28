"""PredictionProvider 契约。

provider 的职责边界：把 `PredictionRequest` 变成原始响应文本 + 元数据。
**不做**解析、不做 TTL 判断、不做并发控制、不做 staleness 判断——那些属于 runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from prediction.types import PredictionRequest, ProviderUsage


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """provider 的返回。

    `raw_response` 是交给 strict parser 的文本；provider 侧证据（requested/resolved model、
    response id、usage）随响应一起回流，最终写入 `PredictionRecord`（P0001.4.2）。
    """

    provider: str
    model: str
    raw_response: str
    requested_model: str | None = None
    resolved_model: str | None = None
    response_id: str | None = None
    usage: ProviderUsage | None = None


@runtime_checkable
class PredictionProvider(Protocol):
    """预测 provider。实现必须是 async，且不得阻塞调用方事件循环。"""

    async def predict(self, request: PredictionRequest) -> ProviderResponse: ...
