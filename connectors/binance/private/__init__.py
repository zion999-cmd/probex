"""Binance USDⓈ-M 私有账户层（P0001.9.2）：只读、无下单能力。

职责：

- 凭据（**只**来自环境变量）与签名（HMAC-SHA256 + server-time offset）；
- 账户 / 持仓快照事实（`AccountSnapshotObservation` / `PositionObservation`）；
- User Data Stream（`ACCOUNT_UPDATE` / `ORDER_TRADE_UPDATE` / `listenKeyExpired`）→ Observation；
- listenKey 生命周期状态机与 private-stream latency telemetry。

明确不做：创建/撤销订单、改杠杆或保证金模式、驱动 `ExecutionEngine`、自动同步 Accounting、
启动对账（属 P0001.9.3）。
"""

from __future__ import annotations

from connectors.binance.private.account import AccountSnapshotObservation, BalanceObservation, parse_account_snapshot
from connectors.binance.private.auth import (
    API_KEY_ENV,
    API_SECRET_ENV,
    ApiCredentials,
    ServerTimeOffset,
    SignedRequest,
    build_signed_request,
    canonical_query,
    redact,
    sanitize_mapping,
    sanitize_query,
    wall_clock_ms,
)
from connectors.binance.private.errors import (
    CredentialsError,
    ListenKeyError,
    PrivateApiError,
    PrivateAuthError,
    PrivateFormatError,
    PrivateResponseError,
    PrivateStreamError,
    ReconnectExhaustedError,
    UnsupportedAccountModeError,
)
from connectors.binance.private.events import (
    AccountUpdateObservation,
    BalanceDeltaObservation,
    ListenKeyExpiredObservation,
    OrderUpdateObservation,
    PositionDeltaObservation,
    UserEvent,
    UserEventOrdering,
    UserEventType,
    parse_user_event,
)
from connectors.binance.private.positions import AccountMode, PositionObservation, parse_position_risk
from connectors.binance.private.rest import API_KEY_HEADER, PrivateRestClient, RestFetcher, UrllibRestFetcher
from connectors.binance.private.runtime import (
    PrivateAccountConfig,
    PrivateAccountRuntime,
    PrivateBatch,
    PrivateSnapshotBoundary,
    latency_summary,
)
from connectors.binance.private.telemetry import (
    LatencyDistribution,
    LatencySamples,
    PrivateStreamCounters,
    PrivateStreamTelemetry,
    latency_distribution,
)
from connectors.binance.private.user_stream import (
    DOCUMENTED_LISTEN_KEY_TTL_MS,
    ListenKeyLifecycle,
    ListenKeyState,
    UserStreamClient,
)

__all__ = [
    "API_KEY_ENV",
    "API_KEY_HEADER",
    "API_SECRET_ENV",
    "AccountMode",
    "AccountSnapshotObservation",
    "AccountUpdateObservation",
    "ApiCredentials",
    "BalanceDeltaObservation",
    "BalanceObservation",
    "CredentialsError",
    "DOCUMENTED_LISTEN_KEY_TTL_MS",
    "LatencyDistribution",
    "LatencySamples",
    "ListenKeyError",
    "ListenKeyExpiredObservation",
    "ListenKeyLifecycle",
    "ListenKeyState",
    "OrderUpdateObservation",
    "PositionDeltaObservation",
    "PositionObservation",
    "PrivateAccountConfig",
    "PrivateAccountRuntime",
    "PrivateApiError",
    "PrivateAuthError",
    "PrivateBatch",
    "PrivateFormatError",
    "PrivateResponseError",
    "PrivateRestClient",
    "PrivateSnapshotBoundary",
    "PrivateStreamCounters",
    "PrivateStreamError",
    "PrivateStreamTelemetry",
    "ReconnectExhaustedError",
    "RestFetcher",
    "ServerTimeOffset",
    "SignedRequest",
    "UnsupportedAccountModeError",
    "UrllibRestFetcher",
    "UserEvent",
    "UserEventOrdering",
    "UserEventType",
    "UserStreamClient",
    "build_signed_request",
    "canonical_query",
    "latency_distribution",
    "latency_summary",
    "parse_account_snapshot",
    "parse_position_risk",
    "parse_user_event",
    "redact",
    "sanitize_mapping",
    "sanitize_query",
    "wall_clock_ms",
]
