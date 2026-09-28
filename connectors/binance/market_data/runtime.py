"""Live 市场数据运行时（P0001.9.1）：把真实公网 WS/REST 接进 canonical pipeline。

```text
public WS (depth)    ─→ depthUpdate → MarketEvent(BOOK_DELTA) ─→ FeatureEngine → MarketState
market WS (aggTrade) ─→ aggTrade    → MarketEvent(TRADE)      ─→ batch（供 fill simulation）
market WS (markPrice)─→ markPrice   → MarkPriceObservation    ─→ batch（独立事实，不进 MarketEvent）
REST                 ─→ depth snapshot → MarketEvent(BOOK_SNAPSHOT) → 对齐 / 重放缓冲
REST                 ─→ exchangeInfo → TradingRules
```

纪律：

- **wall-clock 只出现在这里**（`receive_ts` / `process_ts` / 超时 / backoff）；源事件时间永不被改写；
- 盘口可信度只有一个 Owner（`MarketBook`）：本类只调用既有入口 `request_resync()`，
  并在传输层丢失时调用 `invalidate("transport_lost")`，不自行维护第二份 health；
- malformed 报文 / 单次快照失败：计入 telemetry 并继续运行，**不杀死 runtime**；
- 订阅到错误 tier 会 ACK 但无数据：本类不做静默 fallback，只暴露 telemetry（`message_count` 长时间为 0）。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from market.events.payloads import TradePayload
from market.events.types import MarketEvent, Milliseconds, Venue
from market.features.engine import FeatureEngine
from market.health.state import BookHealth
from market.state.types import MarketState

from connectors.binance.market_data.depth import DEPTH_UPDATE_EVENT, parse_depth_diff
from connectors.binance.market_data.endpoints import REST_BASE_URL, WS_HOST, StreamTier
from connectors.binance.market_data.errors import (
    MarketDataFormatError,
    ReconnectExhaustedError,
    TransportError,
    WebSocketClosed,
    WebSocketTimeout,
)
from connectors.binance.market_data.exchange_info import TradingRules
from connectors.binance.market_data.mark import MARK_PRICE_EVENT, MarkPriceObservation, parse_mark_price
from connectors.binance.market_data.snapshot import (
    DepthSnapshotClient,
    ExchangeInfoClient,
    JsonHttpClient,
    ServerTimeClient,
    UrllibJsonClient,
    wall_clock_ms,
)
from connectors.binance.market_data.streams import (
    agg_trade_stream,
    depth_stream,
    mark_price_stream,
    parse_message,
    stream_matches,
    subscribe_message,
)
from connectors.binance.market_data.trades import AGG_TRADE_EVENT, AggTradeDeduplicator, parse_agg_trade
from connectors.binance.market_data.transport import ReconnectPolicy, WebSocketConnection, connect

#: 传输层丢失的失效原因（审计用；也是 `MarketBook.invalidate` 的 reason）。
TRANSPORT_LOST_REASON = "transport_lost"


class TransportFactory(Protocol):
    """注入式连接工厂（默认 `transport.connect`）。"""

    def __call__(self, url: str, *, timeout_s: float) -> WebSocketConnection: ...


@dataclass(frozen=True, slots=True)
class LiveMarketDataConfig:
    """Live 运行时配置（全部显式）。"""

    symbol: str
    depth_speed: str
    mark_price_speed: str
    depth_limit: int
    connect_timeout_s: float
    read_timeout_s: float
    snapshot_timeout_s: float
    exchange_info_timeout_s: float
    reconnect: ReconnectPolicy
    #: 两次重同步之间的最小间隔（**按市场事件时间**计，避免 gap 风暴打爆 REST 权重）。
    #: 真实实测：`/public` 的 diff 流会跳过 update id，若每条 gap 都重同步会产生 REST 429。
    resync_cooldown_ms: Milliseconds
    #: 保留多少条近期 MarketState（有界，研究用）。
    history_limit: int
    #: 可选：盘口最大年龄门（None = 不做年龄门控）。
    max_book_age_ms: Milliseconds | None = None
    #: WS 主机前缀（2026 迁移后的分层入口；可整体覆盖以应对端点变更）。
    ws_host: str = WS_HOST

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("LiveMarketDataConfig.symbol must be a non-empty string")
        for name in ("connect_timeout_s", "read_timeout_s", "snapshot_timeout_s", "exchange_info_timeout_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"LiveMarketDataConfig.{name} must be a positive number")
        if isinstance(self.depth_limit, bool) or not isinstance(self.depth_limit, int) or not 5 <= self.depth_limit <= 1000:
            raise ValueError("LiveMarketDataConfig.depth_limit must be an int in [5, 1000]")
        if isinstance(self.history_limit, bool) or not isinstance(self.history_limit, int) or self.history_limit < 1:
            raise ValueError("LiveMarketDataConfig.history_limit must be an int >= 1")
        if (
            isinstance(self.resync_cooldown_ms, bool)
            or not isinstance(self.resync_cooldown_ms, int)
            or self.resync_cooldown_ms < 0
        ):
            raise ValueError("LiveMarketDataConfig.resync_cooldown_ms must be an int >= 0")
        if not isinstance(self.reconnect, ReconnectPolicy):
            raise ValueError("LiveMarketDataConfig.reconnect must be a ReconnectPolicy")
        if not isinstance(self.ws_host, str) or not self.ws_host.startswith(("ws://", "wss://")):
            raise ValueError(f"LiveMarketDataConfig.ws_host must start with ws:// or wss://, got {self.ws_host!r}")


@dataclass
class LiveMarketDataCounters:
    """可变计数器（`telemetry` 返回不可变快照）。"""

    ws_connect_count: int = 0
    ws_reconnect_count: int = 0
    ws_disconnect_count: int = 0
    ws_timeout_count: int = 0
    message_count: int = 0
    ack_count: int = 0
    malformed_message_count: int = 0
    depth_event_count: int = 0
    depth_gap_count: int = 0
    state_count: int = 0
    resync_count: int = 0
    snapshot_count: int = 0
    snapshot_failure_count: int = 0
    resync_suppressed_count: int = 0
    snapshot_latency_ms: int | None = None
    agg_trade_count: int = 0
    duplicate_trade_count: int = 0
    mark_update_count: int = 0
    event_lag_ms: int | None = None
    server_time_offset_ms: int | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class LiveMarketDataTelemetry:
    """运行时 telemetry 快照（判定 Live 是否可信的基础指标）。"""

    ws_connect_count: int
    ws_reconnect_count: int
    ws_disconnect_count: int
    ws_timeout_count: int
    message_count: int
    ack_count: int
    malformed_message_count: int
    depth_event_count: int
    depth_gap_count: int
    state_count: int
    resync_count: int
    snapshot_count: int
    snapshot_failure_count: int
    resync_suppressed_count: int
    snapshot_latency_ms: int | None
    agg_trade_count: int
    duplicate_trade_count: int
    mark_update_count: int
    event_lag_ms: int | None
    mark_age_ms: int | None
    server_time_offset_ms: int | None
    #: P0001.9.5 §12：market epoch（invalidate / resync / 重连都会推进；旧执行授权据此失效）
    market_generation: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class MarketDataBatch:
    """一次 `pump_once` 的结果。"""

    market_events: tuple[MarketEvent, ...] = ()
    states: tuple[MarketState, ...] = ()
    mark: MarkPriceObservation | None = None
    errors: tuple[str, ...] = ()
    timed_out: bool = False

    @property
    def empty(self) -> bool:
        return not self.market_events and not self.states and self.mark is None


@dataclass(frozen=True, slots=True)
class RunSummary:
    """`run()` 的汇总（长跑 smoke 用；不保留全部状态）。"""

    telemetry: LiveMarketDataTelemetry
    last_state: MarketState | None
    state_count: int
    messages_processed: int
    stopped_reason: str


@dataclass
class StreamConnection:
    """一条 tier 连接（含订阅 stream 与连接状态）。"""

    tier: StreamTier
    streams: tuple[str, ...]
    ws: WebSocketConnection | None = None

    def url(self, ws_host: str) -> str:
        return f"{self.tier.with_host(ws_host)}/stream?streams={'/'.join(self.streams)}"


class LiveMarketDataRuntime:
    """Binance USDⓈ-M public market data 运行时（只读、无凭据）。"""

    def __init__(
        self,
        *,
        config: LiveMarketDataConfig,
        http_client: JsonHttpClient | None = None,
        transport_factory: TransportFactory | None = None,
        clock: Callable[[], Milliseconds] = wall_clock_ms,
        sleeper: Callable[[float], None] = time.sleep,
        engine: FeatureEngine | None = None,
        venue: Venue = Venue.BINANCE,
    ) -> None:
        self._config = config
        self._clock = clock
        self._sleeper = sleeper
        self._engine = (
            engine
            if engine is not None
            else FeatureEngine(venue, config.symbol, max_book_age_ms=config.max_book_age_ms)
        )
        self._http_client = http_client if http_client is not None else UrllibJsonClient(base_url=REST_BASE_URL)
        self._snapshot_client, self._exchange_info_client, self._server_time_client = _build_clients(
            self._http_client, config=config, clock=clock
        )
        self._transport_factory: TransportFactory = (
            transport_factory if transport_factory is not None else _default_transport_factory
        )
        self._dedup = AggTradeDeduplicator()
        self._counters = LiveMarketDataCounters()
        self._connections = _build_connections(config)
        self._handlers: dict[object, Callable[[dict[str, object], "_BatchBuilder"], None]] = {
            DEPTH_UPDATE_EVENT: self._on_depth,
            AGG_TRADE_EVENT: self._on_agg_trade,
            MARK_PRICE_EVENT: self._on_mark,
        }
        self._history: deque[MarketState] = deque(maxlen=config.history_limit)
        self._latest_mark: MarkPriceObservation | None = None
        self._trading_rules: TradingRules | None = None
        self._resync_needed = True
        self._last_resync_attempt_ms: Milliseconds | None = None
        self._next_request_id = 0
        self._next_connection = 0
        self._market_generation = 0
        self._market_generation_reason = "initial"

    # ------------------------------------------------------------------ 只读状态

    @property
    def config(self) -> LiveMarketDataConfig:
        return self._config

    @property
    def engine(self) -> FeatureEngine:
        return self._engine

    @property
    def latest_mark(self) -> MarkPriceObservation | None:
        return self._latest_mark

    @property
    def trading_rules(self) -> TradingRules | None:
        return self._trading_rules

    @property
    def market_generation(self) -> int:
        """market epoch 计数（P0001.9.5 §12）：任何使盘口可信度改变的路径都会推进它。"""
        return self._market_generation

    @property
    def market_generation_reason(self) -> str:
        return self._market_generation_reason

    def _bump_market_generation(self, reason: str) -> None:
        """推进 market epoch（唯一入口；不得有多处各自 +1 的实现）。"""
        self._market_generation += 1
        self._market_generation_reason = reason

    @property
    def history(self) -> tuple[MarketState, ...]:
        return tuple(self._history)

    @property
    def telemetry(self) -> LiveMarketDataTelemetry:
        counters = self._counters
        mark = self._latest_mark
        return LiveMarketDataTelemetry(
            ws_connect_count=counters.ws_connect_count,
            ws_reconnect_count=counters.ws_reconnect_count,
            ws_disconnect_count=counters.ws_disconnect_count,
            ws_timeout_count=counters.ws_timeout_count,
            message_count=counters.message_count,
            ack_count=counters.ack_count,
            malformed_message_count=counters.malformed_message_count,
            depth_event_count=counters.depth_event_count,
            depth_gap_count=counters.depth_gap_count,
            state_count=counters.state_count,
            resync_count=counters.resync_count,
            snapshot_count=counters.snapshot_count,
            snapshot_failure_count=counters.snapshot_failure_count,
            resync_suppressed_count=counters.resync_suppressed_count,
            snapshot_latency_ms=counters.snapshot_latency_ms,
            agg_trade_count=counters.agg_trade_count,
            duplicate_trade_count=counters.duplicate_trade_count,
            mark_update_count=counters.mark_update_count,
            event_lag_ms=counters.event_lag_ms,
            mark_age_ms=None if mark is None else max(0, self._clock() - mark.receive_ts),
            server_time_offset_ms=counters.server_time_offset_ms,
            market_generation=self._market_generation,
            last_error=counters.last_error,
        )

    # ------------------------------------------------------------------ 生命周期

    def connect(self) -> None:
        """建立两条 tier 连接并发送订阅。"""
        self._bump_market_generation("connect")
        for connection in self._connections:
            connection.ws = self._open(connection)

    def close(self) -> None:
        self._bump_market_generation("close")
        for connection in self._connections:
            if connection.ws is not None:
                connection.ws.close()
                connection.ws = None

    def load_trading_rules(self) -> TradingRules:
        """启动时读取 exchangeInfo（真实 filter 规则，不用 precision 猜）。"""
        self._trading_rules = self._exchange_info_client.fetch()
        return self._trading_rules

    def measure_server_time_offset(self) -> int:
        """测量 `server_time - local_time`（仅观测，**不改写**源事件时间）。"""
        offset = self._server_time_client.fetch_offset_ms()
        self._counters.server_time_offset_ms = offset
        return offset

    # ------------------------------------------------------------------ 主循环

    def pump_once(self, *, timeout_s: float, max_messages: int = 1) -> MarketDataBatch:
        """处理**当前可用**的消息（两条 tier 连接轮转，顺序确定）。

        `max_messages` 让调用方在一次调用里排空积压（默认 1 = 每条消息一次调用，便于确定性测试）：
        真实行情（depth 100ms + 突发 aggTrade）下如果每轮只处理一条，就会积压出任意大的 event lag。

        超时返回 `timed_out=True`；传输错误触发重连；重连用尽抛 `ReconnectExhaustedError`。
        """
        if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages < 1:
            raise ValueError("max_messages must be an int >= 1")
        builder = _BatchBuilder()
        connection = self._next()
        if connection.ws is None:
            raise TransportError(f"tier {connection.tier.value} is not connected; call connect() first")
        for index in range(max_messages):
            if index > 0:
                connection = self._next()
            if not self._pump_one(connection, builder, timeout_s=timeout_s):
                break
        return builder.build(timed_out=builder.timed_out)

    def _pump_one(self, connection: StreamConnection, builder: "_BatchBuilder", *, timeout_s: float) -> bool:
        """处理一条消息；返回 False 表示应当停止本轮排空（超时 / 无数据）。"""
        try:
            text = connection.ws.recv_text(timeout_s=timeout_s) if connection.ws is not None else None
        except WebSocketTimeout:
            self._counters.ws_timeout_count += 1
            builder.timed_out = True
            return False
        except (WebSocketClosed, TransportError) as exc:
            self._counters.last_error = str(exc)
            self._reconnect(connection, builder)
            return False
        if text is None:
            self._reconnect(connection, builder)
            return False
        self._handle_text(connection, text, builder)
        return True

    def run(self, *, max_messages: int, timeout_s: float) -> RunSummary:
        """只读运行：处理至多 `max_messages` 条消息，或到壁钟超时为止。"""
        if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages < 1:
            raise ValueError("max_messages must be an int >= 1")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise ValueError("timeout_s must be a positive number")
        deadline = self._clock() + int(timeout_s * 1000)
        processed = 0
        stopped = "max_messages"
        idle = 0
        # 无进展上限：即使调用方传入常量时钟，也不会空转（例如所有消息都被判为 malformed）
        idle_limit = 4 * len(self._connections)
        while processed < max_messages:
            if self._clock() >= deadline:
                stopped = "timeout"
                break
            if idle >= idle_limit:
                stopped = "no_progress"
                break
            batch = self.pump_once(timeout_s=self._config.read_timeout_s)
            gained = len(batch.market_events) + (1 if batch.mark is not None else 0)
            processed += gained
            idle = 0 if gained > 0 else idle + 1
        return RunSummary(
            telemetry=self.telemetry,
            last_state=self._history[-1] if self._history else None,
            state_count=self._counters.state_count,
            messages_processed=processed,
            stopped_reason=stopped,
        )

    # ------------------------------------------------------------------ 消息处理

    def _handle_text(self, connection: StreamConnection, text: str, builder: "_BatchBuilder") -> None:
        self._counters.message_count += 1
        try:
            message = parse_message(text)
            if message is None:
                self._counters.ack_count += 1
                return
            self._dispatch(connection, message.stream, message.data, builder)
        except MarketDataFormatError as exc:
            self._record_malformed(exc, builder)

    def _dispatch(
        self,
        connection: StreamConnection,
        stream: str | None,
        data: dict[str, object],
        builder: "_BatchBuilder",
    ) -> None:
        if not any(stream_matches(_Envelope(stream), expected) for expected in connection.streams):
            raise MarketDataFormatError(
                f"message for unexpected stream {stream!r} on tier {connection.tier.value}"
            )
        handler = self._handlers.get(data.get("e"))
        if handler is None:
            raise MarketDataFormatError(f"unsupported event type {data.get('e')!r}")
        handler(data, builder)

    def _on_depth(self, raw: dict[str, object], builder: "_BatchBuilder") -> None:
        event = parse_depth_diff(raw, receive_ts=self._clock(), process_ts=self._clock())
        self._counters.depth_event_count += 1
        self._update_lag(event)
        state = self._engine.on_market_event(event)
        self._record_state(state)
        builder.market_events.append(event)
        builder.states.append(state)
        if state.quality.book_health is BookHealth.STALE:
            self._counters.depth_gap_count += 1
            self._resync_needed = True
            self._bump_market_generation("depth_gap")
            self._engine.request_resync()
        if self._resync_needed:
            self._fetch_and_apply_snapshot(builder)

    def _on_agg_trade(self, raw: dict[str, object], builder: "_BatchBuilder") -> None:
        event = parse_agg_trade(
            raw, receive_ts=self._clock(), process_ts=self._clock(), symbol=self._config.symbol
        )
        payload = event.payload
        if not isinstance(payload, TradePayload):
            raise MarketDataFormatError("aggTrade did not produce a TradePayload")
        if not self._dedup.accept(symbol=event.symbol, aggregate_trade_id=payload.aggregate_trade_id):
            self._counters.duplicate_trade_count += 1
            return
        self._counters.agg_trade_count += 1
        self._update_lag(event)
        # trade 不进入 FeatureEngine（TradeFeatures 仍 unavailable），只交给下游（fill simulation）
        builder.market_events.append(event)

    def _on_mark(self, raw: dict[str, object], builder: "_BatchBuilder") -> None:
        observation = parse_mark_price(
            raw, receive_ts=self._clock(), process_ts=self._clock(), symbol=self._config.symbol
        )
        self._latest_mark = observation
        self._counters.mark_update_count += 1
        builder.mark = observation

    # ------------------------------------------------------------------ 快照 / 重连

    def _fetch_and_apply_snapshot(self, builder: "_BatchBuilder") -> None:
        now = self._clock()
        last = self._last_resync_attempt_ms
        cooldown = self._config.resync_cooldown_ms
        if last is not None and cooldown > 0 and now - last < cooldown:
            # 冷却中：保持不可信，等下一批事件再试（不刷 REST）
            self._counters.resync_suppressed_count += 1
            return
        self._last_resync_attempt_ms = now
        self._counters.resync_count += 1
        self._bump_market_generation("resync_applied")
        started = now
        try:
            event = self._snapshot_client.fetch()
        except (TransportError, MarketDataFormatError) as exc:
            self._counters.snapshot_failure_count += 1
            self._counters.last_error = str(exc)
            builder.errors.append(str(exc))
            return
        self._counters.snapshot_count += 1
        self._counters.snapshot_latency_ms = max(0, self._clock() - started)
        state = self._engine.on_market_event(event)
        self._record_state(state)
        builder.market_events.append(event)
        builder.states.append(state)
        self._resync_needed = state.quality.book_health is not BookHealth.HEALTHY

    def _reconnect(self, connection: StreamConnection, builder: "_BatchBuilder") -> None:
        self._counters.ws_disconnect_count += 1
        if connection.ws is not None:
            connection.ws.close()
            connection.ws = None
        policy = self._config.reconnect
        for attempt in range(1, policy.max_attempts + 1):
            self._sleeper(policy.delay_ms(attempt) / 1000.0)
            try:
                connection.ws = self._open(connection)
            except TransportError as exc:
                self._counters.last_error = str(exc)
                continue
            self._counters.ws_reconnect_count += 1
            self._after_transport_loss()
            return
        detail = f"reconnect exhausted for tier {connection.tier.value} after {policy.max_attempts} attempts"
        builder.errors.append(detail)
        self._counters.last_error = detail
        raise ReconnectExhaustedError(detail)

    def _after_transport_loss(self) -> None:
        """传输层丢失：盘口立即不可信，必须重新完成 snapshot + 对齐才恢复（SC-9）。"""
        self._engine.invalidate(TRANSPORT_LOST_REASON)
        self._bump_market_generation("transport_lost")
        if self._engine.book_health is BookHealth.STALE:
            self._engine.request_resync()
        self._resync_needed = True
        self._last_resync_attempt_ms = None  # 重连后立即允许一次重同步

    def _open(self, connection: StreamConnection) -> WebSocketConnection:
        ws = self._transport_factory(connection.url(self._config.ws_host), timeout_s=self._config.connect_timeout_s)
        self._counters.ws_connect_count += 1
        self._next_request_id += 1
        ws.send_text(subscribe_message(connection.streams, request_id=self._next_request_id))
        return ws

    # ------------------------------------------------------------------ 内部

    def _next(self) -> StreamConnection:
        connection = self._connections[self._next_connection % len(self._connections)]
        self._next_connection += 1
        return connection

    def _record_state(self, state: MarketState) -> None:
        self._history.append(state)
        self._counters.state_count += 1

    def _update_lag(self, event: MarketEvent) -> None:
        self._counters.event_lag_ms = event.process_ts - event.exchange_ts

    def _record_malformed(self, error: BaseException, builder: "_BatchBuilder") -> None:
        self._counters.malformed_message_count += 1
        self._counters.last_error = str(error)
        builder.errors.append(str(error))


@dataclass(frozen=True, slots=True)
class _Envelope:
    """`stream_matches` 只依赖 `stream` 字段，这里做最小适配。"""

    stream: str | None


@dataclass
class _BatchBuilder:
    """一次 pump 的累积器。"""

    market_events: list[MarketEvent] = field(default_factory=list)
    states: list[MarketState] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    mark: MarkPriceObservation | None = None
    timed_out: bool = False

    def build(self, *, timed_out: bool) -> MarketDataBatch:
        return MarketDataBatch(
            market_events=tuple(self.market_events),
            states=tuple(self.states),
            mark=self.mark,
            errors=tuple(self.errors),
            timed_out=timed_out,
        )


def _build_connections(config: LiveMarketDataConfig) -> tuple[StreamConnection, StreamConnection]:
    """按 2026 tier 规则分配连接：depth → public；aggTrade + markPrice → market。"""
    return (
        StreamConnection(
            tier=StreamTier.PUBLIC,
            streams=(depth_stream(config.symbol, speed=config.depth_speed),),
        ),
        StreamConnection(
            tier=StreamTier.MARKET,
            streams=(
                agg_trade_stream(config.symbol),
                mark_price_stream(config.symbol, speed=config.mark_price_speed),
            ),
        ),
    )


def _build_clients(
    http_client: JsonHttpClient, *, config: LiveMarketDataConfig, clock: Callable[[], Milliseconds]
) -> tuple[DepthSnapshotClient, ExchangeInfoClient, ServerTimeClient]:
    """装配三个 REST 客户端（共享同一个注入式 HTTP 客户端）。"""
    snapshot = DepthSnapshotClient(
        client=http_client,
        symbol=config.symbol,
        limit=config.depth_limit,
        timeout_s=config.snapshot_timeout_s,
        clock=clock,
    )
    exchange_info = ExchangeInfoClient(
        client=http_client, symbol=config.symbol, timeout_s=config.exchange_info_timeout_s
    )
    server_time = ServerTimeClient(
        client=http_client, timeout_s=config.exchange_info_timeout_s, clock=clock
    )
    return snapshot, exchange_info, server_time


def _default_transport_factory(url: str, *, timeout_s: float) -> WebSocketConnection:
    return connect(url, timeout_s=timeout_s)


__all__ = [
    "LiveMarketDataConfig",
    "LiveMarketDataCounters",
    "LiveMarketDataRuntime",
    "LiveMarketDataTelemetry",
    "MarketDataBatch",
    "RunSummary",
    "StreamConnection",
    "TRANSPORT_LOST_REASON",
    "TransportFactory",
]
