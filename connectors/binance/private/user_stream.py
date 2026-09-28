"""User Data Stream：listenKey 状态机 + WS 客户端（P0001.9.2 §0.5）。

状态机（显式转换表，非法转换抛错）：

```text
STOPPED --start_creating--> STARTING --on_created--> ACTIVE
ACTIVE --mark_renewing--> RENEWING --on_renewed--> ACTIVE
ACTIVE --mark_reconnecting--> RECONNECTING --on_reconnected--> ACTIVE
ACTIVE/RENEWING/RECONNECTING --on_expired--> EXPIRED --start_creating--> STARTING
any(except STOPPED/FAILED) --fail--> FAILED
any --stop--> STOPPED
```

listenKey 本身视为敏感：`ListenKeyLifecycle` 的 `repr` / `sanitized_view()` 都不含其内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping

from market.events.types import Milliseconds

from connectors.binance.market_data.endpoints import WS_HOST, StreamTier
from connectors.binance.market_data.transport import WebSocketConnection
from connectors.binance.private.errors import ListenKeyError

#: 官方文档给出的 listenKey 生命周期（60 分钟）。真实 TTL 由配置注入，不用默认值。
DOCUMENTED_LISTEN_KEY_TTL_MS = 60 * 60 * 1000


class ListenKeyState(Enum):
    """listenKey 生命周期状态。"""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    RENEWING = "RENEWING"
    EXPIRED = "EXPIRED"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"


#: 允许的转换（同态视为幂等 no-op，不算合法转换）。
ALLOWED_TRANSITIONS: Mapping[ListenKeyState, frozenset[ListenKeyState]] = {
    ListenKeyState.STOPPED: frozenset({ListenKeyState.STARTING}),
    ListenKeyState.STARTING: frozenset({ListenKeyState.ACTIVE, ListenKeyState.FAILED, ListenKeyState.STOPPED}),
    ListenKeyState.ACTIVE: frozenset(
        {
            ListenKeyState.RENEWING,
            ListenKeyState.EXPIRED,
            ListenKeyState.RECONNECTING,
            ListenKeyState.FAILED,
            ListenKeyState.STOPPED,
        }
    ),
    ListenKeyState.RENEWING: frozenset(
        {ListenKeyState.ACTIVE, ListenKeyState.EXPIRED, ListenKeyState.FAILED, ListenKeyState.STOPPED}
    ),
    ListenKeyState.EXPIRED: frozenset({ListenKeyState.STARTING, ListenKeyState.FAILED, ListenKeyState.STOPPED}),
    ListenKeyState.RECONNECTING: frozenset(
        {ListenKeyState.ACTIVE, ListenKeyState.EXPIRED, ListenKeyState.FAILED, ListenKeyState.STOPPED}
    ),
    ListenKeyState.FAILED: frozenset({ListenKeyState.STOPPED, ListenKeyState.STARTING}),
}


def transition_allowed(source: ListenKeyState, target: ListenKeyState) -> bool:
    return target in ALLOWED_TRANSITIONS[source]


@dataclass
class ListenKeyLifecycle:
    """listenKey 生命周期状态机（不含 listenKey 文本的输出路径）。"""

    state: ListenKeyState = ListenKeyState.STOPPED
    ttl_ms: Milliseconds = DOCUMENTED_LISTEN_KEY_TTL_MS
    created_at_ms: Milliseconds | None = None
    last_keepalive_ms: Milliseconds | None = None
    expires_at_ms: Milliseconds | None = None
    create_count: int = 0
    renew_count: int = 0
    expired_count: int = 0
    failure_reason: str | None = None
    _listen_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.ttl_ms, bool) or not isinstance(self.ttl_ms, int) or self.ttl_ms <= 0:
            raise ListenKeyError(f"ttl_ms must be a positive int, got {self.ttl_ms!r}")

    def __repr__(self) -> str:
        return (
            f"ListenKeyLifecycle(state={self.state.value}, has_key={self._listen_key is not None}, "
            f"create_count={self.create_count}, renew_count={self.renew_count}, "
            f"expired_count={self.expired_count})"
        )

    __str__ = __repr__

    @property
    def listen_key(self) -> str | None:
        """listenKey 原文（只在需要建立连接时取用；**不要打印/上报**）。"""
        return self._listen_key

    @property
    def has_key(self) -> bool:
        return self._listen_key is not None

    def sanitized_view(self) -> dict[str, object]:
        """可安全写入 telemetry / 日志的视图。"""
        return {
            "state": self.state.value,
            "has_key": self.has_key,
            "create_count": self.create_count,
            "renew_count": self.renew_count,
            "expired_count": self.expired_count,
        }

    # ------------------------------------------------------------------ 转换

    def _transition(self, target: ListenKeyState) -> None:
        if target is self.state:
            return
        if not transition_allowed(self.state, target):
            raise ListenKeyError(f"illegal listenKey transition: {self.state.value} -> {target.value}")
        self.state = target

    def start_creating(self, *, now_ms: Milliseconds) -> None:
        self._transition(ListenKeyState.STARTING)
        self.created_at_ms = now_ms

    def on_created(self, listen_key: str, *, now_ms: Milliseconds) -> None:
        if not isinstance(listen_key, str) or not listen_key:
            raise ListenKeyError("listenKey must be a non-empty string")
        self._transition(ListenKeyState.ACTIVE)
        self._listen_key = listen_key
        self.create_count += 1
        self.last_keepalive_ms = now_ms
        self.expires_at_ms = now_ms + self.ttl_ms
        self.failure_reason = None

    def mark_renewing(self) -> None:
        self._transition(ListenKeyState.RENEWING)

    def on_renewed(self, *, now_ms: Milliseconds) -> None:
        self._transition(ListenKeyState.ACTIVE)
        self.renew_count += 1
        self.last_keepalive_ms = now_ms
        self.expires_at_ms = now_ms + self.ttl_ms

    def mark_reconnecting(self) -> None:
        """socket 断开但 listenKey 可能仍有效（不改 key，不重置 TTL）。"""
        self._transition(ListenKeyState.RECONNECTING)

    def on_reconnected(self) -> None:
        self._transition(ListenKeyState.ACTIVE)

    def on_expired(self, *, now_ms: Milliseconds, reason: str = "listenKeyExpired") -> None:
        self._transition(ListenKeyState.EXPIRED)
        self.expired_count += 1
        self._listen_key = None
        self.expires_at_ms = None
        self.failure_reason = reason

    def fail(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ListenKeyError("failure reason must be a non-empty string")
        if self.state in (ListenKeyState.STOPPED, ListenKeyState.FAILED):
            raise ListenKeyError(f"cannot fail a listenKey lifecycle in state {self.state.value}")
        self._transition(ListenKeyState.FAILED)
        self.failure_reason = reason

    def stop(self) -> None:
        if self.state is ListenKeyState.STOPPED:
            return
        self._transition(ListenKeyState.STOPPED)
        self._listen_key = None
        self.expires_at_ms = None

    # ------------------------------------------------------------------ 判定

    def is_expired(self, *, now_ms: Milliseconds) -> bool:
        if self.state is not ListenKeyState.ACTIVE and self.state is not ListenKeyState.RECONNECTING:
            return False
        return self.expires_at_ms is not None and now_ms >= self.expires_at_ms

    def needs_keepalive(self, *, now_ms: Milliseconds, interval_ms: Milliseconds) -> bool:
        if self.state is not ListenKeyState.ACTIVE:
            return False
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or interval_ms <= 0:
            raise ListenKeyError(f"interval_ms must be a positive int, got {interval_ms!r}")
        if self.last_keepalive_ms is None:
            return True
        return now_ms - self.last_keepalive_ms >= interval_ms


@dataclass
class UserStreamClient:
    """user data stream 的 WS 侧（传输注入；URL 由 endpoints 单一 Owner 生成）。"""

    transport_factory: Callable[..., WebSocketConnection]
    ws_host: str = WS_HOST
    tier: StreamTier = StreamTier.PRIVATE

    def url(self, listen_key: str) -> str:
        return self.tier.user_data_url(listen_key, ws_host=self.ws_host)

    def sanitized_url(self, listen_key: str) -> str:
        return self.tier.sanitized_user_data_url(listen_key, ws_host=self.ws_host)

    def connect(self, listen_key: str, *, timeout_s: float) -> WebSocketConnection:
        """建立 user data stream 连接（user stream 无需 SUBSCRIBE，事件自动推送）。"""
        return self.transport_factory(self.url(listen_key), timeout_s=timeout_s)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "DOCUMENTED_LISTEN_KEY_TTL_MS",
    "ListenKeyLifecycle",
    "ListenKeyState",
    "UserStreamClient",
    "transition_allowed",
]
