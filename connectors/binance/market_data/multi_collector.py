"""多标的行情采集编排（P0017.1 §3）。

**不是新连接器、不引入 CCXT、不做多 venue**：在既有 `LiveMarketDataRuntime`
（P0001.9.1，含 snapshot 对齐 / pu 连续性 / resync / 重连 / 每标的 FeatureEngine）之上，
为每个订阅标的持有一个**独立 runtime 实例**，在一个采集线程上轮转 pump。

隔离性
------
- 一个标的的畸形报文 / resync / 重连只发生在它自己的 runtime，不触碰其他标的状态。
- 每标的独立健康/时间戳/错误/持久化水位；编排层只做调度与事实搬运。
- 连接预算：每 runtime 2 条 WS（public/market tier）；订阅数受 `max_symbols` 约束。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from connectors.binance.market_data.endpoints import (
    REST_BASE_URL,
    WS_HOST,
)
from connectors.binance.market_data.runtime import (
    LiveMarketDataConfig,
    LiveMarketDataRuntime,
)
from connectors.binance.market_data.snapshot import UrllibJsonClient
from connectors.binance.market_data.transport import ReconnectPolicy, connect as default_connect
from market.events.types import Milliseconds, Venue


@dataclass(frozen=True, slots=True)
class Subscription:
    """一个被订阅标的的采集状态快照（不可变）。"""

    symbol: str
    subscribed: bool
    healthy: bool
    message_count: int
    state_count: int
    resync_count: int
    last_error: str | None
    last_event_exchange_ts: Milliseconds | None
    market_generation: int

    def view(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "status": ("HEALTHY" if self.healthy else ("SUBSCRIBING" if self.subscribed else "IDLE")),
            "message_count": self.message_count,
            "state_count": self.state_count,
            "resync_count": self.resync_count,
            "last_event_exchange_ts": self.last_event_exchange_ts,
            "market_generation": self.market_generation,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    symbols: tuple[str, ...]
    max_symbols: int = 16
    pump_timeout_s: float = 0.5
    max_messages: int = 64
    rest_base: str = REST_BASE_URL
    ws_host: str = WS_HOST
    max_book_age_ms: Milliseconds = 5_000
    history_limit: int = 128

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("CollectorConfig.symbols must not be empty")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("CollectorConfig.symbols must be unique")
        if len(self.symbols) > self.max_symbols:
            raise ValueError(
                f"subscription {len(self.symbols)} exceeds max_symbols {self.max_symbols}")


class MultiSymbolMarketCollector:
    """轮转驱动多个单标的 runtime（采集线程；只读，无 broker / 无下单能力）。"""

    def __init__(
        self,
        config: CollectorConfig,
        *,
        clock: Callable[[], Milliseconds] | None = None,
        http_client_factory: Callable[[str], object] | None = None,
        transport_factory: Callable[..., object] | None = None,
        on_batch: Callable[[str, object], None] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._http_factory = http_client_factory or (
            lambda base: UrllibJsonClient(base_url=base))
        self._transport_factory = transport_factory or default_connect
        self._on_batch = on_batch
        self._runtimes: dict[str, LiveMarketDataRuntime] = {}
        self._connected = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_error: str | None = None

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._config.symbols

    def connect(self) -> None:
        """构造每个标的的 runtime、读取交易规则并建立 WS（失败如实抛出）。"""
        reconnect = ReconnectPolicy(max_attempts=5, base_backoff_ms=500, max_backoff_ms=5_000)
        for symbol in self._config.symbols:
            runtime = LiveMarketDataRuntime(
                config=LiveMarketDataConfig(
                    symbol=symbol, depth_speed="100ms", mark_price_speed="1s", depth_limit=100,
                    connect_timeout_s=10.0, read_timeout_s=0.5, snapshot_timeout_s=10.0,
                    exchange_info_timeout_s=10.0, reconnect=reconnect,
                    resync_cooldown_ms=1_000, history_limit=self._config.history_limit,
                    ws_host=self._config.ws_host, max_book_age_ms=self._config.max_book_age_ms),
                http_client=self._http_factory(self._config.rest_base),
                transport_factory=lambda url, timeout_s: self._transport_factory(url, timeout_s=timeout_s),
                clock=self._clock,
                venue=Venue.BINANCE)
            runtime.load_trading_rules()
            self._runtimes[symbol] = runtime
            runtime.connect()
        self._connected = True

    def pump_once(self) -> dict[str, object]:
        """每个标的 pump 一轮（非线程安全；离线测试可直接调用）。返回每标的最新 batch 摘要。"""
        out: dict[str, object] = {}
        for symbol, runtime in self._runtimes.items():
            try:
                batch = runtime.pump_once(timeout_s=self._config.pump_timeout_s,
                                           max_messages=self._config.max_messages)
            except Exception as exc:  # noqa: BLE001 - 单标的故障隔离
                out[symbol] = {"error": type(exc).__name__}
                continue
            out[symbol] = {
                "states": len(batch.states), "events": len(batch.market_events),
                "errors": batch.errors, "has_mark": batch.mark is not None,
            }
            if self._on_batch is not None:
                try:
                    self._on_batch(symbol, batch)
                except Exception:  # noqa: BLE001 - sink 失败不影响采集
                    pass
        return out

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self._connected:
            self.connect()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                         name="probex-multi-market", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        for runtime in self._runtimes.values():
            try:
                runtime.close()
            except Exception:  # noqa: BLE001
                pass
        self._connected = False

    def _run(self) -> None:
        while not self._stop.is_set():
            summary = self.pump_once()
            for result in summary.values():
                if isinstance(result, dict) and "error" in result:
                    self._last_error = str(result["error"])

    # ------------------------------------------------------------------ 只读状态

    def runtime(self, symbol: str) -> LiveMarketDataRuntime | None:
        return self._runtimes.get(symbol)

    def latest_state(self, symbol: str) -> object | None:
        runtime = self._runtimes.get(symbol)
        if runtime is None:
            return None
        history = runtime.history
        return history[-1] if history else None

    def subscriptions(self) -> tuple[Subscription, ...]:
        result = []
        for symbol in self._config.symbols:
            runtime = self._runtimes.get(symbol)
            if runtime is None:
                result.append(Subscription(symbol, False, False, 0, 0, 0, None, None, 0))
                continue
            telemetry = runtime.telemetry
            history = runtime.history
            last_state = history[-1] if history else None
            healthy = False
            last_ts: Milliseconds | None = None
            if last_state is not None:
                healthy = str(last_state.quality.book_health.value).lower() == "healthy"
                last_ts = int(last_state.time.as_of_exchange_ts)
            result.append(Subscription(
                symbol=symbol, subscribed=self._connected, healthy=healthy,
                message_count=telemetry.message_count, state_count=telemetry.state_count,
                resync_count=telemetry.resync_count, last_error=telemetry.last_error,
                last_event_exchange_ts=last_ts,
                market_generation=telemetry.market_generation))
        return tuple(result)

    @property
    def last_error(self) -> str | None:
        return self._last_error


__all__ = ["CollectorConfig", "MultiSymbolMarketCollector", "Subscription"]
