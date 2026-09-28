"""私有账户运行时（P0001.9.2）：只读、无下单能力。

```text
凭据（env only）→ server-time offset → listenKey → user stream
              → account snapshot + position snapshot → snapshot boundary
              → ACCOUNT_UPDATE / ORDER_TRADE_UPDATE / listenKeyExpired（只产出 Observation）
```

纪律：

- **不创建/撤销订单**（本模块没有、也不调用任何下单端点；SC-14 由静态测试固定）；
- 只产出事实：不写 OrderTracker / Accounting / ExecutionEngine（对账属 P0001.9.3）；
- 重连或 listenKey 重建后**不假设状态连续**（`continuity_assumed=False`），
  必须重新拉取 snapshot 才恢复（由调用方触发或下一次 `refresh_snapshot()`）；
- wall-clock 只出现在这里（keepalive 调度、lag 计算）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from market.events.types import Milliseconds

from connectors.binance.market_data.endpoints import WS_HOST, StreamTier
from connectors.binance.market_data.errors import TransportError, WebSocketClosed, WebSocketTimeout
from connectors.binance.market_data.transport import ReconnectPolicy, WebSocketConnection
from connectors.binance.private.account import AccountSnapshotObservation, parse_account_snapshot
from connectors.binance.private.auth import ApiCredentials, ClockCalibration, ServerTimeOffset, wall_clock_ms
from connectors.binance.private.errors import (
    CredentialsError,
    ListenKeyError,
    PrivateApiError,
    PrivateFormatError,
    PrivateStreamError,
    ReconnectExhaustedError,
    UnsupportedAccountModeError,
)
from connectors.binance.private.events import (
    AccountUpdateObservation,
    OrderUpdateObservation,
    UserEvent,
    UserEventOrdering,
    UserEventType,
    parse_user_event,
)
from connectors.binance.private.positions import PositionObservation, parse_position_risk
from connectors.binance.private.rest import PrivateRestClient
from connectors.binance.private.telemetry import (
    LatencyDistribution,
    LatencySamples,
    PrivateStreamCounters,
    PrivateStreamTelemetry,
)
from connectors.binance.private.user_stream import ListenKeyLifecycle, UserStreamClient


class TransportFactory(Protocol):
    def __call__(self, url: str, *, timeout_s: float) -> WebSocketConnection: ...


@dataclass(frozen=True, slots=True)
class PrivateAccountConfig:
    """私有运行时配置（全部显式；阈值无默认值）。"""

    symbol: str
    recv_window_ms: int
    listen_key_ttl_ms: Milliseconds
    keepalive_interval_ms: Milliseconds
    connect_timeout_s: float
    read_timeout_s: float
    request_timeout_s: float
    #: SC-13：private stream median receive-lag 的验收阈值（毫秒）。
    max_median_private_lag_ms: Milliseconds
    latency_sample_limit: int
    reconnect: ReconnectPolicy
    ws_host: str = WS_HOST

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise PrivateFormatError("PrivateAccountConfig.symbol must be a non-empty string")
        for name in ("connect_timeout_s", "read_timeout_s", "request_timeout_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise PrivateFormatError(f"PrivateAccountConfig.{name} must be a positive number")
        for name in (
            "recv_window_ms",
            "listen_key_ttl_ms",
            "keepalive_interval_ms",
            "latency_sample_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PrivateFormatError(f"PrivateAccountConfig.{name} must be a positive int")
        if self.max_median_private_lag_ms < 0:
            raise PrivateFormatError("PrivateAccountConfig.max_median_private_lag_ms must be >= 0")
        if self.keepalive_interval_ms >= self.listen_key_ttl_ms:
            raise PrivateFormatError("keepalive_interval_ms must be < listen_key_ttl_ms")
        if not isinstance(self.reconnect, ReconnectPolicy):
            raise PrivateFormatError("PrivateAccountConfig.reconnect must be a ReconnectPolicy")
        if not isinstance(self.ws_host, str) or not self.ws_host.startswith(("ws://", "wss://")):
            raise PrivateFormatError(f"PrivateAccountConfig.ws_host must start with ws:// or wss://, got {self.ws_host!r}")


@dataclass(frozen=True, slots=True)
class PrivateSnapshotBoundary:
    """快照边界：user stream 建立之后拉取的账户/持仓快照，供 P0001.9.3 对账使用。

    `stream_connected_at_ms` 是 **WS 实际连接成功**的时刻（不是快照开始时刻）；
    `snapshot_started_at_ms` 单独记录本轮快照的开始时刻。
    """

    symbol: str
    stream_connected_at_ms: Milliseconds
    snapshot_started_at_ms: Milliseconds
    account_received_ts: Milliseconds
    position_received_ts: Milliseconds
    account_update_time_ms: Milliseconds | None
    position_update_time_ms: Milliseconds | None


@dataclass(frozen=True, slots=True)
class PrivateBatch:
    """一次 `pump_once` 的结果。"""

    events: tuple[UserEvent, ...] = ()
    snapshot: AccountSnapshotObservation | None = None
    errors: tuple[str, ...] = ()
    timed_out: bool = False

    @property
    def empty(self) -> bool:
        return not self.events and self.snapshot is None


@dataclass
class PrivateAccountRuntime:
    """只读私有账户运行时。"""

    config: PrivateAccountConfig
    rest: PrivateRestClient
    stream: UserStreamClient
    #: 凭据缺失时由调用方传 None（或让 `ApiCredentials.from_env()` 抛错）——两者都 fail closed。
    credentials: ApiCredentials | None = None
    clock: Callable[[], Milliseconds] = wall_clock_ms
    sleeper: Callable[[float], None] = time.sleep
    offset: ServerTimeOffset = field(default_factory=ServerTimeOffset)

    def __post_init__(self) -> None:
        self.lifecycle = ListenKeyLifecycle(ttl_ms=self.config.listen_key_ttl_ms)
        self.counters = PrivateStreamCounters()
        self.ordering = UserEventOrdering()
        self.latency = LatencySamples(limit=self.config.latency_sample_limit)
        self.raw_latency = LatencySamples(limit=self.config.latency_sample_limit)
        self._ws: WebSocketConnection | None = None
        self._stream_connected_at_ms: Milliseconds | None = None
        self._boundary: PrivateSnapshotBoundary | None = None
        self._latest_snapshot: AccountSnapshotObservation | None = None
        self._latest_position: PositionObservation | None = None
        self._continuity_assumed = False
        self._calibration: ClockCalibration | None = None
        self._discontinuity_listeners: list[Callable[[str], None]] = []
        self._discontinuity_events: list[str] = []

    # ------------------------------------------------------------------ 只读状态

    @property
    def lifecycle_state(self) -> str:
        return self.lifecycle.state.value

    @property
    def continuity_assumed(self) -> bool:
        """是否可在「已知快照边界」之上假设事件连续（重连/重建 listenKey 后为 False）。"""
        return self._continuity_assumed

    @property
    def snapshot_boundary(self) -> PrivateSnapshotBoundary | None:
        return self._boundary

    @property
    def latest_snapshot(self) -> AccountSnapshotObservation | None:
        return self._latest_snapshot

    @property
    def latest_position(self) -> PositionObservation | None:
        return self._latest_position

    @property
    def clock_calibration(self) -> ClockCalibration | None:
        """最近一次 server-time 校准（含 RTT / 不确定度 / 测量时刻）。

        来源是 `PrivateRestClient.measure_clock()` 的返回值（单一来源），未测量时为 None。
        """
        return self._calibration

    def refresh_clock_calibration(self) -> ClockCalibration:
        """重新测量时钟校准（readiness 需要新鲜的不确定度，而不是沿用启动时的一次测量）。"""
        calibration = self.rest.measure_clock()
        self._calibration = calibration
        self.counters.server_time_offset_ms = calibration.offset_ms
        return calibration

    @property
    def telemetry(self) -> PrivateStreamTelemetry:
        counters = self.counters
        return PrivateStreamTelemetry(
            listen_key_state=self.lifecycle.state.value,
            continuity_assumed=self._continuity_assumed,
            last_receive_lag_ms=self.latency.last,
            private_lag_ms=self.latency.distribution(),
            last_raw_receive_lag_ms=self.raw_latency.last,
            raw_private_lag_ms=self.raw_latency.distribution(),
            uncorrected_lag_sample_count=counters.uncorrected_lag_sample_count,
            clock_calibration=self.clock_calibration,
            connect_count=counters.connect_count,
            reconnect_count=counters.reconnect_count,
            disconnect_count=counters.disconnect_count,
            timeout_count=counters.timeout_count,
            message_count=counters.message_count,
            heartbeat_count=counters.heartbeat_count,
            account_update_count=counters.account_update_count,
            order_update_count=counters.order_update_count,
            fill_observation_count=counters.fill_observation_count,
            listen_key_expired_count=counters.listen_key_expired_count,
            listen_key_created_count=counters.listen_key_created_count,
            keepalive_count=counters.keepalive_count,
            keepalive_failure_count=counters.keepalive_failure_count,
            duplicate_count=counters.duplicate_count,
            out_of_order_count=counters.out_of_order_count,
            unsupported_event_count=counters.unsupported_event_count,
            malformed_count=counters.malformed_count,
            discontinuity_listener_failure_count=counters.discontinuity_listener_failure_count,
            snapshot_count=counters.snapshot_count,
            snapshot_failure_count=counters.snapshot_failure_count,
            snapshot_round_trip_ms=counters.snapshot_round_trip_ms,
            server_time_offset_ms=counters.server_time_offset_ms,
            last_error=counters.last_error,
        )

    @property
    def discontinuity_events(self) -> tuple[str, ...]:
        """真实发生过的连续性丢失事件（P0001.9.3.1 SC-4 的可审计证据）。"""
        return tuple(self._discontinuity_events)

    def subscribe_discontinuity(self, listener: Callable[[str], None]) -> None:
        """订阅「stream 连续性丢失」事件（断线重连 / listenKey 重建 / 停止）。

        P0001.9.3.1 SC-4：`StartupRecovery` 通过该契约**自动**失效，而不是靠调用方记得手工调用
        `invalidate()`。listener 收到的人类可读 reason 只含事件类型，不含任何凭据。
        """
        if not callable(listener):
            raise PrivateFormatError("subscribe_discontinuity(listener) requires a callable")
        self._discontinuity_listeners.append(listener)

    def _notify_discontinuity(self, reason: str) -> None:
        """记录并广播一次连续性丢失（P0001.9.3.2 §0.1）。

        顺序与纪律：

        1. **先记录事实**（`discontinuity_events`），保证即使所有 listener 都失败也有审计证据；
        2. 每个 listener **独立** `try`，一个失败不影响其他 listener；
        3. 异常**绝不向上抛**，以免破坏 `_reconnect` / `_recreate_listen_key` / `stop()` 的既有语义；
        4. 失败只记 `discontinuity_listener_failure_count` + 异常**类型名**（不记消息/参数/堆栈，
           避免任何敏感内容进入 telemetry）。

        只捕获 `Exception`：`KeyboardInterrupt` / `SystemExit` 等 `BaseException` 照常传播。
        """
        self._discontinuity_events.append(reason)
        for listener in list(self._discontinuity_listeners):
            try:
                listener(reason)
            except Exception as exc:  # noqa: BLE001 —— observer 边界：逐个隔离，绝不外泄
                self.counters.discontinuity_listener_failure_count += 1
                self.counters.last_error = f"discontinuity listener failed: {type(exc).__name__}"

    def lag_within_threshold(self) -> bool:
        """SC-13 判定：已采样且 median 不超过配置阈值（样本不足视为未达标，不猜）。"""
        distribution = self.latency.distribution()
        if distribution is None:
            return False
        return distribution.median <= self.config.max_median_private_lag_ms

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> PrivateSnapshotBoundary:
        """启动顺序（提案 §0.6）：凭据 → server time → listenKey → user stream → snapshot → boundary。"""
        self._require_credentials()
        self.counters.server_time_offset_ms = self.refresh_clock_calibration().offset_ms
        self._create_listen_key_and_connect()
        self.refresh_snapshot()
        assert self._boundary is not None  # refresh_snapshot 已设置
        return self._boundary

    def refresh_snapshot(self) -> PrivateSnapshotBoundary:
        """拉取 account + position 快照并建立新的 boundary（同时恢复 continuity 假设）。

        失败**不吞**：计数后原样上抛（调用方决定是否重试）；不伪造快照、不假设状态连续。
        """
        self._require_credentials()
        try:
            return self._fetch_and_apply_snapshot()
        except (TransportError, PrivateApiError) as exc:
            self.counters.snapshot_failure_count += 1
            self._continuity_assumed = False
            self.counters.last_error = f"snapshot failed: {type(exc).__name__}"
            raise

    def _fetch_and_apply_snapshot(self) -> PrivateSnapshotBoundary:
        snapshot_started = int(self.clock())
        account_raw = self.rest.account_snapshot()
        account_received = int(self.clock())
        position_raw = self.rest.position_risk()
        position_received = int(self.clock())
        self.counters.snapshot_count += 1

        snapshot = parse_account_snapshot(
            account_raw, symbol=self.config.symbol, receive_ts=account_received, process_ts=position_received
        )
        if snapshot.settlement_balance is None:
            # 账户资产列表里没有 USDT 条目 ⇒ 无法确认 USDT-M（fail closed，不自动兼容）
            raise UnsupportedAccountModeError(
                f"{self.config.symbol}: account has no USDT asset entry; "
                "P0001.9.2 only supports USDT-margined accounts"
            )
        position = parse_position_risk(
            position_raw, symbol=self.config.symbol, receive_ts=position_received, process_ts=int(self.clock())
        )
        self._latest_snapshot = snapshot
        self._latest_position = position
        self.counters.snapshot_round_trip_ms = max(0, position_received - snapshot_started)
        if self._stream_connected_at_ms is None:
            raise PrivateStreamError(
                "snapshot boundary requires a connected user stream (connect the stream before snapshotting)"
            )
        self._boundary = PrivateSnapshotBoundary(
            symbol=self.config.symbol,
            stream_connected_at_ms=self._stream_connected_at_ms,
            snapshot_started_at_ms=snapshot_started,
            account_received_ts=account_received,
            position_received_ts=position_received,
            account_update_time_ms=snapshot.update_time_ms,
            position_update_time_ms=position.update_time_ms,
        )
        self._continuity_assumed = True
        return self._boundary

    def stop(self) -> None:
        """best-effort 幂等关闭：WS 必须关闭；`DELETE listenKey` 失败只记录错误，本地仍进入 `STOPPED`。"""
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None
        if self.lifecycle.has_key and self.credentials is not None:
            try:
                self.rest.listen_key_close()
            except (TransportError, PrivateApiError) as exc:
                self.counters.last_error = f"listenKey close failed: {type(exc).__name__}"
        self._stream_connected_at_ms = None
        self._continuity_assumed = False
        self.lifecycle.stop()
        self._notify_discontinuity("runtime stopped")

    def pump_once(self, *, timeout_s: float, max_messages: int = 1) -> PrivateBatch:
        """处理当前可用的事件（含 keepalive 调度）；超时返回 `timed_out=True`。"""
        if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages < 1:
            raise PrivateFormatError("max_messages must be an int >= 1")
        builder = _BatchBuilder()
        self._maintain_listen_key(builder)
        for _ in range(max_messages):
            if not self._pump_one(builder, timeout_s=timeout_s):
                break
        return builder.build(timed_out=builder.timed_out)

    # ------------------------------------------------------------------ 内部

    def _require_credentials(self) -> ApiCredentials:
        if self.credentials is None:
            raise CredentialsError(
                "private runtime requires credentials (set BINANCE_API_KEY / BINANCE_API_SECRET); refusing to start"
            )
        return self.credentials

    def _measure_server_time(self) -> int:
        return self.rest.measure_server_time()

    def _create_listen_key_and_connect(self) -> None:
        now = int(self.clock())
        self.lifecycle.start_creating(now_ms=now)
        try:
            listen_key = self.rest.listen_key_create()
        except (TransportError, PrivateApiError) as exc:
            self.counters.last_error = f"listenKey create failed: {type(exc).__name__}"
            self.lifecycle.fail("listenKey create failed")
            raise
        self.lifecycle.on_created(listen_key, now_ms=int(self.clock()))
        self.counters.listen_key_created_count += 1
        self._connect_ws(listen_key)

    def _connect_ws(self, listen_key: str) -> None:
        self._ws = self.stream.connect(listen_key, timeout_s=self.config.connect_timeout_s)
        self._stream_connected_at_ms = int(self.clock())  # 真实 WS 连接成功时刻
        self.counters.connect_count += 1

    def _maintain_listen_key(self, builder: "_BatchBuilder") -> None:
        """keepalive / 过期处理（按注入时钟；不使用后台线程）。"""
        now = int(self.clock())
        if self.lifecycle.is_expired(now_ms=now):
            self._recreate_listen_key(builder, reason="listenKey TTL elapsed without keepalive")
            return
        if self.lifecycle.needs_keepalive(now_ms=now, interval_ms=self.config.keepalive_interval_ms):
            self.lifecycle.mark_renewing()
            try:
                self.rest.listen_key_keepalive()
            except (TransportError, PrivateApiError) as exc:
                # 任何失败（含 HTTP 4xx/5xx）都必须离开 RENEWING：绝不留下僵尸状态
                self.counters.keepalive_failure_count += 1
                self.counters.last_error = f"keepalive failed: {type(exc).__name__}"
                self.lifecycle.fail("keepalive failed")
                raise ListenKeyError("listenKey keepalive failed") from None
            self.lifecycle.on_renewed(now_ms=int(self.clock()))
            self.counters.keepalive_count += 1

    def _recreate_listen_key(self, builder: "_BatchBuilder", *, reason: str) -> None:
        """listenKey 失效（TTL 或 listenKeyExpired）：重建 + 重连，**不假设状态连续**。"""
        self.counters.listen_key_expired_count += 1
        self._continuity_assumed = False
        self._notify_discontinuity(f"listenKey recreated: {reason}")
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        if self.lifecycle.state.value != "EXPIRED":
            self.lifecycle.on_expired(now_ms=int(self.clock()), reason=reason)
        builder.errors.append(f"listenKey recreated: {reason}")
        self._create_listen_key_and_connect()

    def _sync_heartbeats(self) -> None:
        """把传输层观测到的 ping 数同步到 telemetry（SC-6 的心跳证据）。"""
        if self._ws is not None:
            self.counters.heartbeat_count = self._ws.ping_count

    def _pump_one(self, builder: "_BatchBuilder", *, timeout_s: float) -> bool:
        if self._ws is None:
            raise PrivateStreamError("user data stream is not connected; call start() first")
        self._sync_heartbeats()
        try:
            text = self._ws.recv_text(timeout_s=timeout_s)
        except WebSocketTimeout:
            self.counters.timeout_count += 1
            builder.timed_out = True
            return False
        except (WebSocketClosed, TransportError) as exc:
            self.counters.last_error = f"stream error: {type(exc).__name__}"
            self._reconnect(builder)
            return False
        if text is None:
            self._reconnect(builder)
            return False
        self._handle_text(text, builder)
        return True

    def _handle_text(self, text: str, builder: "_BatchBuilder") -> None:
        self.counters.message_count += 1
        receive_ts = int(self.clock())
        try:
            raw = _decode_json(text)
            event = parse_user_event(raw, receive_ts=receive_ts, process_ts=int(self.clock()))
        except PrivateFormatError as exc:
            self.counters.malformed_count += 1
            self.counters.last_error = "malformed user stream message"
            builder.errors.append(str(exc))
            return
        if not event.supported:
            self.counters.unsupported_event_count += 1
            return
        if event.event_type is UserEventType.LISTEN_KEY_EXPIRED:
            self._recreate_listen_key(builder, reason="listenKeyExpired event")
            return
        if not self.ordering.accept(event):
            self.counters.duplicate_count = self.ordering.duplicate_count
            self.counters.out_of_order_count = self.ordering.out_of_order_count
            return
        self._record_event(event, receive_ts=receive_ts)
        builder.events.append(event)

    def _record_event(self, event: UserEvent, *, receive_ts: Milliseconds) -> None:
        observation = event.observation
        if isinstance(observation, AccountUpdateObservation):
            self.counters.account_update_count += 1
            self._record_lag(event_ts=observation.event_ts, receive_ts=receive_ts)
        elif isinstance(observation, OrderUpdateObservation):
            self.counters.order_update_count += 1
            if observation.is_fill:
                self.counters.fill_observation_count += 1
            self._record_lag(event_ts=observation.event_ts, receive_ts=receive_ts)

    def _record_lag(self, *, event_ts: Milliseconds, receive_ts: Milliseconds) -> None:
        """记录事件延迟（P0001.9.4 §6 / D-036）。

        - 原始差值 `receive_ts - event_ts` 只作审计（它混入了本地与交易所的时钟偏差）；
        - **主指标**是校正值 `receive_ts - event_ts + offset_ms`（offset = 交易所 − 本地），
          且必须能同时给出 `uncertainty_ms`；
        - 校准不可用时**不**产生校正样本，也没有 anything 被当成 0（未知 ≠ 0）。
        """
        raw = receive_ts - event_ts
        self.raw_latency.add(raw)
        calibration = self.clock_calibration
        if calibration is None:
            self.counters.uncorrected_lag_sample_count += 1
            return
        self.latency.add(calibration.corrected_lag_ms(receive_ts=receive_ts, event_ts=event_ts))

    def _reconnect_once(self) -> None:
        """一次重连尝试：复用未过期的 listenKey，否则重建。"""
        if self.lifecycle.has_key and self.lifecycle.listen_key is not None:
            if self.lifecycle.state.value == "ACTIVE":
                self.lifecycle.mark_reconnecting()
            self._connect_ws(self.lifecycle.listen_key)
            if self.lifecycle.state.value == "RECONNECTING":
                self.lifecycle.on_reconnected()
            return
        self._create_listen_key_and_connect()

    def _reconnect(self, builder: "_BatchBuilder") -> None:
        """socket 断开：重连（复用未过期的 listenKey），并放弃 continuity 假设。"""
        self.counters.disconnect_count += 1
        self._continuity_assumed = False
        self._notify_discontinuity("user data stream disconnected; reconnecting")
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        policy = self.config.reconnect
        for attempt in range(1, policy.max_attempts + 1):
            self.sleeper(policy.delay_ms(attempt) / 1000.0)
            try:
                self._reconnect_once()
            except (TransportError, PrivateApiError) as exc:
                self.counters.last_error = f"reconnect failed: {type(exc).__name__}"
                continue
            self.counters.reconnect_count += 1
            return
        detail = f"reconnect exhausted after {policy.max_attempts} attempts"
        builder.errors.append(detail)
        self.counters.last_error = detail
        raise ReconnectExhaustedError(detail)


@dataclass
class _BatchBuilder:
    events: list[UserEvent] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    snapshot: AccountSnapshotObservation | None = None
    timed_out: bool = False

    def build(self, *, timed_out: bool) -> PrivateBatch:
        return PrivateBatch(
            events=tuple(self.events), snapshot=self.snapshot, errors=tuple(self.errors), timed_out=timed_out
        )


def _decode_json(text: str) -> object:
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrivateFormatError(f"user stream message is not valid JSON: {exc}") from None


def latency_summary(distribution: LatencyDistribution | None) -> dict[str, int] | None:
    """把 latency 分布转成可打印字典（供 live smoke 报告）。"""
    if distribution is None:
        return None
    return {
        "samples": distribution.samples,
        "min": distribution.minimum,
        "median": distribution.median,
        "p95": distribution.p95,
        "max": distribution.maximum,
    }


__all__ = [
    "PrivateAccountConfig",
    "PrivateAccountRuntime",
    "PrivateBatch",
    "PrivateSnapshotBoundary",
    "latency_summary",
]
