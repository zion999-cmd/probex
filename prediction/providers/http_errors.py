"""HTTP 状态 → 既有 failure type 的共享映射。

provider transport 共用同一套判定，避免不同 provider 给出不一致的失败语义：

- 4xx（400/401/403/404/405/406/409/410/413/415/422 及其它 4xx）→ `PredictionTransportError`
  （请求 / 凭证被拒，provider 没有作答）
- 408 / 425 / 429 / 5xx（TypeSafe 的 529 Overloaded 亦属此类）→ `PredictionProviderError`
  （服务端限流或故障，交由上层 backoff 处理）
- 其余（1xx/3xx 等异常）→ `PredictionTransportError`
"""

from __future__ import annotations

from prediction.errors import PredictionError, PredictionProviderError, PredictionTransportError

_CLIENT_ERROR_STATUSES = frozenset({400, 401, 403, 404, 405, 406, 409, 410, 413, 415, 422})

_PROVIDER_ERROR_STATUSES = frozenset({408, 425, 429})


def map_http_status(status: int, *, response_excerpt: str = "", provider: str = "provider") -> PredictionError:
    """把非 2xx HTTP 状态映射为既有 failure type（绝不伪造 Prediction）。"""
    detail = f"; body excerpt: {response_excerpt[:500]}" if response_excerpt else ""
    if status in _PROVIDER_ERROR_STATUSES or status >= 500:
        return PredictionProviderError(f"{provider} returned HTTP {status}{detail}")
    if status in _CLIENT_ERROR_STATUSES or 400 <= status < 500:
        return PredictionTransportError(f"{provider} rejected the request with HTTP {status}{detail}")
    return PredictionTransportError(f"{provider} returned unexpected HTTP {status}{detail}")


__all__ = ["map_http_status"]
