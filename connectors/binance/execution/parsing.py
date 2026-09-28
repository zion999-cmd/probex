"""Binance 写执行路径的报文解析与**结果三分类**（P0001.9.6 §5 / §8）。

最重要的一条不变量：

```text
只有交易所**明确**给出可解析的业务结果，才允许 CONFIRMED_ACCEPTED / CONFIRMED_REJECTED；
其余一切（超时 / 连接中断 / 无法解析 / 5xx）都是 UNKNOWN —— 绝不生成 FAILED。
```

UNKNOWN 必须由上层按"不确定暴露"处理（`LOST` + query/reconciliation），且**禁止**自动重试 submit。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from market.events.types import Milliseconds

from connectors.binance.market_data.errors import TransportError
from connectors.binance.market_data.parsing import (
    require_field,
    require_int,
    require_mapping,
    require_str,
)
from connectors.binance.private.errors import PrivateResponseError

from connectors.binance.execution.rest import ExecutionOutcomeUnknown, ExecutionRequestRejected

#: Binance 订单状态字符串 → 本地语义（显式映射，未知即报错；不做"大概"判断）。
_ORDER_STATUSES = frozenset(
    {"NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED", "NEW_INSURANCE"}
)


class ExecutionParseError(PrivateResponseError):
    """写路径响应无法按契约解析（⇒ 调用方必须按 UNKNOWN 处理，不得猜成功/失败）。"""


class SubmitClassification(Enum):
    """submit 结果的**唯一**三分类（§5）。"""

    CONFIRMED_ACCEPTED = "CONFIRMED_ACCEPTED"
    CONFIRMED_REJECTED = "CONFIRMED_REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SubmitAcknowledgement:
    """已确认 ACK 的可解析事实。"""

    client_order_id: str
    exchange_order_id: str
    status: str
    symbol: str
    price: float
    original_quantity: float
    executed_quantity: float
    average_price: float
    reduce_only: bool
    post_only: bool
    update_time_ms: Milliseconds | None
    transaction_time_ms: Milliseconds | None


@dataclass(frozen=True, slots=True)
class SubmitRejection:
    """交易所**明确**业务拒绝（4xx + 业务错误码，且请求确实被处理）。"""

    code: int
    message: str
    client_order_id: str | None


@dataclass(frozen=True, slots=True)
class OrderQueryFacts:
    """`GET /fapi/v1/order` 的事实（用于解决 uncertain submit/cancel，§7）。"""

    client_order_id: str
    exchange_order_id: str
    symbol: str
    status: str
    price: float
    original_quantity: float
    executed_quantity: float
    average_price: float
    reduce_only: bool
    update_time_ms: Milliseconds | None


def parse_submit_response(raw: object, *, client_order_id: str) -> SubmitAcknowledgement:
    """解析 ACK（`POST /fapi/v1/order`）。字段缺失/类型错误 ⇒ `ExecutionParseError`（⇒ UNKNOWN）。"""
    item = require_mapping(raw, path="order")
    try:
        status = require_str(require_field(item, "status", path="order"), path="order.status")
        if status not in _ORDER_STATUSES:
            raise ExecutionParseError(f"order.status: unknown status {status!r}")
        order_id = require_int(require_field(item, "orderId", path="order"), path="order.orderId")
        symbol = require_str(require_field(item, "symbol", path="order"), path="order.symbol")
        response_client_id = require_str(
            require_field(item, "clientOrderId", path="order"), path="order.clientOrderId"
        )
        if response_client_id != client_order_id:
            raise ExecutionParseError(
                "order.clientOrderId does not match the submitted clientOrderId (refusing to guess)"
            )
        return SubmitAcknowledgement(
            client_order_id=response_client_id,
            exchange_order_id=str(order_id),
            status=status,
            symbol=symbol,
            price=float(require_field(item, "price", path="order")),
            original_quantity=float(require_field(item, "origQty", path="order")),
            executed_quantity=float(require_field(item, "executedQty", path="order")),
            average_price=float(item.get("avgPrice", 0.0) or 0.0),
            reduce_only=bool(item.get("reduceOnly", False)),
            post_only=str(item.get("timeInForce", "")).upper() == "GTX",
            update_time_ms=_optional_int(item.get("updateTime")),
            transaction_time_ms=_optional_int(item.get("transactTime")),
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionParseError(f"order response field is not usable: {type(exc).__name__}") from None


def parse_order_query(raw: object, *, client_order_id: str) -> OrderQueryFacts:
    """解析 `GET /fapi/v1/order` 的事实。"""
    item = require_mapping(raw, path="order")
    try:
        status = require_str(require_field(item, "status", path="order"), path="order.status")
        if status not in _ORDER_STATUSES:
            raise ExecutionParseError(f"order.status: unknown status {status!r}")
        return OrderQueryFacts(
            client_order_id=require_str(
                require_field(item, "clientOrderId", path="order"), path="order.clientOrderId"
            ),
            exchange_order_id=str(require_int(require_field(item, "orderId", path="order"), path="order.orderId")),
            symbol=require_str(require_field(item, "symbol", path="order"), path="order.symbol"),
            status=status,
            price=float(require_field(item, "price", path="order")),
            original_quantity=float(require_field(item, "origQty", path="order")),
            executed_quantity=float(require_field(item, "executedQty", path="order")),
            average_price=float(item.get("avgPrice", 0.0) or 0.0),
            reduce_only=bool(item.get("reduceOnly", False)),
            update_time_ms=_optional_int(item.get("updateTime")),
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionParseError(f"order query field is not usable: {type(exc).__name__}") from None


def classify_submit_response(error: BaseException) -> SubmitClassification:
    """把异常映射成**三分类**（§5，结构化判定，不做字符串匹配）。

    - `ExecutionRequestRejected`（HTTP 4xx + 业务错误码，交易所**已处理**请求）= CONFIRMED_REJECTED；
    - `ExecutionOutcomeUnknown` / 传输错误 / 解析失败 / 任何其他异常 = UNKNOWN（⇒ 不 retry、query 收敛）。
    """
    if isinstance(error, ExecutionRequestRejected):
        return SubmitClassification.CONFIRMED_REJECTED
    return SubmitClassification.UNKNOWN


def parse_rejection(error: BaseException, *, client_order_id: str) -> SubmitRejection:
    """从**已确认**的业务拒绝里取 `code` / `msg`（不含 signed query，§17）。"""
    if not isinstance(error, ExecutionRequestRejected):
        raise ExecutionParseError("parse_rejection requires a confirmed ExecutionRequestRejected")
    return SubmitRejection(code=error.code, message=error.message, client_order_id=client_order_id)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionParseError(f"expected an int, got {type(value).__name__}")
    return value


__all__ = [
    "ExecutionParseError",
    "OrderQueryFacts",
    "SubmitAcknowledgement",
    "SubmitClassification",
    "SubmitRejection",
    "ExecutionOutcomeUnknown",
    "ExecutionRequestRejected",
    "classify_submit_response",
    "parse_order_query",
    "parse_rejection",
    "parse_submit_response",
]
