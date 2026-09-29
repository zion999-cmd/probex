"""P0001.9.6 测试脚手架：假 Binance 写路径（可注入 ACK / 拒绝 / UNKNOWN / 丢响应）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from connectors.binance.execution.rest import (
    BinanceExecutionRestClient,
    ExecutionOutcomeUnknown,
    ExecutionRequestRejected,
)
from connectors.binance.market_data.transport import ReconnectPolicy  # noqa: F401 - 语义一致
from connectors.binance.private.auth import ApiCredentials
from connectors.binance.private.auth import ServerTimeOffset
from tests.support import BASE_TS

FAKE_API_KEY = "test-execution-key-not-a-credential"
FAKE_API_SECRET = "test-execution-secret-not-a-credential"


def credentials() -> ApiCredentials:
    return ApiCredentials(FAKE_API_KEY, _api_secret=FAKE_API_SECRET)


@dataclass
class FakeExecutionFetcher:
    """脚本化写路径传输：按方法返回预置结果（可模拟丢响应后的真实状态）。"""

    #: 预置响应（按 method），例如 {"POST": {...}}；值为 Exception 时抛出
    responses: dict[str, object] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    #: 仅记录引用：用于断言请求里携带了 clientOrderId（幂等主键）
    seen_queries: list[str] = field(default_factory=list)

    def send(self, *, method: str, url: str, headers: dict[str, str], timeout_s: float) -> object:
        self.calls.append((method, url.split("?")[0]))
        self.seen_queries.append(url)
        result = self.responses.get(method)
        if callable(result):
            # 允许按请求回显（例如把 newClientOrderId / origClientOrderId 放进响应）
            result = result(url)
        if isinstance(result, BaseException):
            raise result
        if result is None:
            raise ExecutionOutcomeUnknown(status=None, detail="no stub response")
        return result


def client(
    fetcher: FakeExecutionFetcher,
    *,
    base_url: str = "https://demo-fapi.binance.com",
    now_ms: int | None = None,
) -> BinanceExecutionRestClient:
    return BinanceExecutionRestClient(
        credentials=credentials(),
        fetcher=fetcher,
        base_url=base_url,
        recv_window_ms=5_000,
        timeout_s=5.0,
        offset=ServerTimeOffset(),
        clock=lambda: (BASE_TS if now_ms is None else now_ms),
    )


def client_order_id_from_url(url: str) -> str:
    """从写请求 URL 里取出客户端订单 id（`newClientOrderId` 优先，其次 `origClientOrderId`）。"""
    import urllib.parse

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    for key in ("newClientOrderId", "origClientOrderId"):
        values = query.get(key)
        if values:
            return values[0]
    raise ValueError("url carries neither newClientOrderId nor origClientOrderId")


def echoing_ack(url: str, *, status: str = "NEW") -> dict:
    """回显 clientOrderId 的 ACK（多单场景下避免"响应指向别人的订单"）。"""
    return ack_payload(client_order_id=client_order_id_from_url(url), status=status)


def echoing_cancel(url: str) -> dict:
    return ack_payload(client_order_id=client_order_id_from_url(url), status="CANCELED")


def ack_payload(
    *,
    client_order_id: str = "probex-s1-000001",
    order_id: int = 4242,
    status: str = "NEW",
    executed_qty: str = "0",
    avg_price: str = "0.0",
) -> dict:
    return {
        "orderId": order_id,
        "symbol": "BTCUSDT",
        "status": status,
        "clientOrderId": client_order_id,
        "price": "60000.0",
        "origQty": "0.002",
        "executedQty": executed_qty,
        "avgPrice": avg_price,
        "timeInForce": "GTX",
        "type": "LIMIT",
        "reduceOnly": False,
        "updateTime": BASE_TS,
        "transactTime": BASE_TS,
    }


__all__ = [
    "FAKE_API_KEY",
    "FAKE_API_SECRET",
    "FakeExecutionFetcher",
    "ack_payload",
    "client",
    "client_order_id_from_url",
    "credentials",
    "echoing_ack",
    "echoing_cancel",
]
