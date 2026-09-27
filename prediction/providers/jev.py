"""JevProvider：把 canonical payload 交给注入的传输函数。

本阶段**不实现** HTTP / 网络客户端，也不管理 API Key：真实传输需要单独的授权
（新依赖 + endpoint 契约）。这里只固化 provider 契约与元数据：

- 输入是冻结好的 `PredictionRequest.payload_json`（canonical payload）。
- 输出是原始响应文本，解析与校验由 runtime + strict parser 负责。
- 传输层失败一律映射为 `PredictionTransportError`，未预期异常映射为
  `PredictionProviderError`，绝不吞掉失败。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from prediction.errors import (
    PredictionError,
    PredictionInvalidResponseError,
    PredictionProviderError,
    PredictionTransportError,
)
from prediction.providers.base import ProviderResponse
from prediction.types import PredictionRequest

#: 传输函数：接收请求，返回原始响应文本。
JevTransport = Callable[[PredictionRequest], Awaitable[str]]


class JevProvider:
    """Jev provider adapter。"""

    PROVIDER_NAME = "jev"

    def __init__(self, transport: JevTransport, *, model: str) -> None:
        if not callable(transport):
            raise ValueError("transport must be callable")
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string")
        self._transport = transport
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def predict(self, request: PredictionRequest) -> ProviderResponse:
        try:
            raw_response = await self._transport(request)
        except PredictionError:
            raise
        except Exception as exc:  # 传输边界：任何底层异常都归为 transport 失败
            raise PredictionTransportError(f"jev transport failed: {type(exc).__name__}: {exc}") from exc

        if not isinstance(raw_response, str) or not raw_response.strip():
            raise PredictionInvalidResponseError("jev transport returned an empty response")

        return ProviderResponse(provider=self.PROVIDER_NAME, model=self._model, raw_response=raw_response)

    async def __call__(self, request: PredictionRequest) -> ProviderResponse:
        return await self.predict(request)


__all__ = [
    "JevProvider",
    "JevTransport",
    "PredictionProviderError",
]
