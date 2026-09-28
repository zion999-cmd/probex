"""Binance USDⓈ-M **写执行**边界（P0001.9.6）。

与 `connectors/binance/private/`（只读账户 / user stream / recovery）显式分开：

```text
private   = READ   （account / positionRisk / openOrders / allOrders / userTrades / income / listenKey）
execution = WRITE  （POST order / DELETE order / GET order by clientOrderId）
```

纪律（提案 §1 / §16 / §17）：

- adapter 只做 `Order → Binance wire request` 与 `Binance 事实 → ExecutionEvent`，**不做任何交易决策**；
- 不 import Jev / Prediction / MakerPolicy / RiskGate（由静态测试固定）；
- 签名 query、API key、signature **绝不**出现在异常/日志/telemetry/repr；
- 第一版只允许 `LIMIT` + `GTX`(post-only) + `positionSide=BOTH`，不自动 round、不自动重试 submit。
"""

from __future__ import annotations

from connectors.binance.execution.adapter import (
    BinanceExecutionAdapter,
    ExecutionAdapterError,
    ExecutionAuthorityContext,
    ExternalFactsProvider,
    ExternalFactsUnavailableError,
    LOCAL_REJECTION_PREFIX,
    SubmitOutcome,
    SubmitRefusedError,
)
from connectors.binance.execution.parsing import (
    ExecutionParseError,
    OrderQueryFacts,
    SubmitAcknowledgement,
    SubmitClassification,
    SubmitRejection,
    classify_submit_response,
    parse_order_query,
    parse_rejection,
    parse_submit_response,
)
from connectors.binance.execution.rest import (
    ORDER_PATH,
    BinanceExecutionRestClient,
    ExecutionHttpFetcher,
    ExecutionOutcomeUnknown,
    ExecutionRequestRejected,
    OrderSide,
    UrllibExecutionFetcher,
)

__all__ = [
    "ORDER_PATH",
    "BinanceExecutionAdapter",
    "BinanceExecutionRestClient",
    "ExecutionAdapterError",
    "ExecutionAuthorityContext",
    "ExecutionHttpFetcher",
    "ExternalFactsProvider",
    "ExternalFactsUnavailableError",
    "ExecutionOutcomeUnknown",
    "ExecutionParseError",
    "ExecutionRequestRejected",
    "LOCAL_REJECTION_PREFIX",
    "OrderQueryFacts",
    "OrderSide",
    "SubmitAcknowledgement",
    "SubmitClassification",
    "SubmitOutcome",
    "SubmitRefusedError",
    "SubmitRejection",
    "UrllibExecutionFetcher",
    "classify_submit_response",
    "parse_order_query",
    "parse_rejection",
    "parse_submit_response",
]
