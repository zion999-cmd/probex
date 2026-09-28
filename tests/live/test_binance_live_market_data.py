"""P0001.9.1 live smoke（opt-in，**不需要任何凭据**）：真实 Binance USDⓈ-M 公网行情。

启用方式：

```bash
PROBEX_LIVE_SMOKE=1 python3 -m unittest -v tests.live.test_binance_live_market_data
```

可选参数（默认见下）：

```bash
PROBEX_LIVE_SMOKE_SECONDS=40      # 观察窗口（秒）
PROBEX_LIVE_SMOKE_MESSAGES=100000 # 安全上限：最多处理的消息数（正常情况下由窗口决定）
PROBEX_LIVE_SMOKE_MIN_DEPTH=10    # 窗口内至少观测到的 depth 事件数
PROBEX_LIVE_SMOKE_MIN_LAG=10      # 至少采样的 event lag 数
PROBEX_LIVE_SMOKE_MAX_MARK_AGE=15000  # mark age 观测上限（毫秒）：证明 mark 在持续刷新
# event lag 上限（毫秒）：用于捕捉**系统性积压**（修前曾达 36s）。经共享代理出口时
# 该值包含代理排队时间（已由独立 tight-loop 探针证明链路自身中位约 −0.2s），因此默认取宽松值。
PROBEX_LIVE_SMOKE_MAX_LAG_MS=30000    # event lag 上限（毫秒）
PROBEX_LIVE_SMOKE_DRAIN=64        # 每次 pump 排空的消息数（防止积压放大 lag）
```

覆盖：

- SC-1 真实 depth → canonical depth events；SC-11 exchange + receive 时间戳保留；
- SC-2 REST snapshot + buffered delta → HEALTHY（并报告 first HEALTHY latency）；
- SC-5/SC-6 真实 aggTrade → TradePayload（含去重计数）；
- SC-7 真实 mark price 独立更新（报告 mark age）；
- SC-8 真实 exchangeInfo → TradingRules；SC-12 无凭据完成全部表面；
- SC-13 持续观察窗口 + 完整 telemetry 汇总（gap / reconnect / resync / malformed / lag 分布）。

若窗口内**没有**自然发生 gap / reconnect，则对应指标记 0，不伪造。
"""

from __future__ import annotations

import json
import os
import statistics
import time
import unittest
from dataclasses import dataclass

from market.health.state import BookHealth

from connectors.binance.market_data.endpoints import (
    DEPTH_PATH,
    EXCHANGE_INFO_PATH,
    REST_BASE_URL,
    SERVER_TIME_PATH,
    WS_HOST,
    StreamTier,
)
from connectors.binance.market_data.runtime import LiveMarketDataRuntime
from connectors.binance.market_data.snapshot import UrllibJsonClient
from connectors.binance.market_data.streams import agg_trade_stream, depth_stream, mark_price_stream
from connectors.binance.market_data.transport import ReconnectPolicy
from tests.live_support import BINANCE_SYMBOL, live_config

#: 需要人类显式打开（避免默认触网）。
LIVE_FLAG = "PROBEX_LIVE_SMOKE"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise AssertionError(f"{name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise AssertionError(f"{name} must be > 0, got {value}")
    return value


def live_enabled() -> bool:
    return os.environ.get(LIVE_FLAG) == "1"


@unittest.skipUnless(live_enabled(), f"set {LIVE_FLAG}=1 to run the real Binance live smoke test")
class BinanceLiveSmokeTest(unittest.TestCase):
    """真实公网 smoke：只读、无凭据。"""

    def _runtime(self) -> LiveMarketDataRuntime:
        return LiveMarketDataRuntime(
            config=live_config(
                symbol=BINANCE_SYMBOL,
                connect_timeout_s=20.0,
                read_timeout_s=15.0,
                snapshot_timeout_s=20.0,
                exchange_info_timeout_s=20.0,
                reconnect=ReconnectPolicy(max_attempts=3, base_backoff_ms=500, max_backoff_ms=4000),
                resync_cooldown_ms=_env_int("PROBEX_LIVE_SMOKE_RESYNC_COOLDOWN_MS", 1000),
                history_limit=4096,
            ),
            http_client=UrllibJsonClient(base_url=REST_BASE_URL),
        )

    def test_sc12_no_credentials_are_required(self) -> None:
        for name in ("BINANCE_API_KEY", "BINANCE_SECRET_KEY", "BINANCE_API_SECRET", "OPENROUTER_API_KEY"):
            self.assertIsNone(os.environ.get(name), f"{name} must not be needed for the public smoke test")

    def test_sc1_sc2_sc5_sc6_sc7_sc8_sc11_sc12_sc13_live_smoke(self) -> None:
        window_seconds = _env_int("PROBEX_LIVE_SMOKE_SECONDS", 40)
        runtime = self._runtime()
        runtime.connect()
        self.addCleanup(runtime.close)

        rules = runtime.load_trading_rules()
        server_time_offset = runtime.measure_server_time_offset()
        samples = _collect(runtime, window_seconds=window_seconds)
        report = _build_report(
            runtime, rules=rules, server_time_offset=server_time_offset, samples=samples
        )
        print("\nPROBEX LIVE SMOKE REPORT:\n" + json.dumps(report, indent=2))

        _assert_sc12(report, self)
        _assert_sc13(report, window_seconds=window_seconds, case=self)


@dataclass(frozen=True, slots=True)
class _Samples:
    """smoke 窗口内采集到的原始观测量。"""

    lags: tuple[int, ...]
    mark_ages: tuple[int, ...]
    first_healthy_ms: int | None
    healthy_states: int
    messages: int
    duration_s: float


def _collect(runtime: LiveMarketDataRuntime, *, window_seconds: int) -> _Samples:
    """驱动只读运行窗口，采集 lag / mark age / 健康状态等观测样本。"""
    max_messages = _env_int("PROBEX_LIVE_SMOKE_MESSAGES", 100_000)
    drain = _env_int("PROBEX_LIVE_SMOKE_DRAIN", 64)
    started = time.monotonic()
    deadline = started + window_seconds
    lags: list[int] = []
    mark_ages: list[int] = []
    first_healthy_ms: int | None = None
    healthy_states = 0
    messages = 0
    while messages < max_messages and time.monotonic() < deadline:
        batch = runtime.pump_once(timeout_s=0.05, max_messages=drain)
        messages += len(batch.market_events) + (1 if batch.mark is not None else 0)
        lags.extend(event.process_ts - event.exchange_ts for event in batch.market_events)
        for state in batch.states:
            if state.quality.book_health is BookHealth.HEALTHY:
                healthy_states += 1
                if first_healthy_ms is None:
                    first_healthy_ms = int((time.monotonic() - started) * 1000)
        mark_age = runtime.telemetry.mark_age_ms
        if mark_age is not None:
            mark_ages.append(mark_age)
    return _Samples(
        lags=tuple(lags),
        mark_ages=tuple(mark_ages),
        first_healthy_ms=first_healthy_ms,
        healthy_states=healthy_states,
        messages=messages,
        duration_s=time.monotonic() - started,
    )


def _build_report(runtime: LiveMarketDataRuntime, *, rules, server_time_offset: int, samples: _Samples) -> dict:
    telemetry = runtime.telemetry
    return {
        "rest": {
            "base_url": REST_BASE_URL,
            "depth_path": DEPTH_PATH,
            "exchange_info_path": EXCHANGE_INFO_PATH,
            "server_time_path": SERVER_TIME_PATH,
            "snapshot_calls": telemetry.snapshot_count,
        },
        "ws": {
            "host": WS_HOST,
            "public_tier_url": StreamTier.PUBLIC.combined_stream_url(
                (depth_stream(BINANCE_SYMBOL, speed=runtime.config.depth_speed),)
            ),
            "market_tier_url": StreamTier.MARKET.combined_stream_url(
                (
                    agg_trade_stream(BINANCE_SYMBOL),
                    mark_price_stream(BINANCE_SYMBOL, speed=runtime.config.mark_price_speed),
                )
            ),
        },
        "exchange_info": {
            "symbol": rules.symbol,
            "status": rules.status,
            "tick_size": rules.tick_size,
            "step_size": rules.step_size,
            "min_notional": rules.min_notional,
        },
        "server_time_offset_ms": server_time_offset,
        "counts": {
            "depth_messages": telemetry.depth_event_count,
            "agg_trade_messages": telemetry.agg_trade_count,
            "mark_price_messages": telemetry.mark_update_count,
            "duplicate_agg_trades": telemetry.duplicate_trade_count,
            "ws_messages": telemetry.message_count,
            "acks": telemetry.ack_count,
        },
        "health": {
            "first_healthy_ms": samples.first_healthy_ms,
            "healthy_states": samples.healthy_states,
            "book_health_last": None if not runtime.history else runtime.history[-1].quality.book_health.value,
        },
        "snapshot_latency_ms": telemetry.snapshot_latency_ms,
        "event_lag_ms": _distribution(list(samples.lags)),
        "mark_age_ms": _distribution(list(samples.mark_ages)),
        "faults": {
            "gaps": telemetry.depth_gap_count,
            "resyncs": telemetry.resync_count,
            "resyncs_suppressed": telemetry.resync_suppressed_count,
            "snapshot_failures": telemetry.snapshot_failure_count,
            "reconnects": telemetry.ws_reconnect_count,
            "disconnects": telemetry.ws_disconnect_count,
            "timeouts": telemetry.ws_timeout_count,
            "malformed": telemetry.malformed_message_count,
        },
        "smoke_duration_s": round(samples.duration_s, 3),
        "messages_processed": samples.messages,
        "last_error": telemetry.last_error,
    }


def _assert_sc12(report: dict, case: unittest.TestCase) -> None:
    """SC-12：无凭据完成 REST snapshot / exchangeInfo / server time / depth / aggTrade / markPrice。"""
    counts = report["counts"]
    case.assertGreaterEqual(report["rest"]["snapshot_calls"], 1, report)
    case.assertTrue(report["exchange_info"]["status"] == "TRADING", report)
    case.assertIsNotNone(report["server_time_offset_ms"], report)
    case.assertGreater(counts["depth_messages"], 0, report)
    case.assertGreater(counts["agg_trade_messages"], 0, report)
    case.assertGreater(counts["mark_price_messages"], 0, report)


def _assert_sc13(report: dict, *, window_seconds: int, case: unittest.TestCase) -> None:
    """SC-13：持续观察窗口 + 真实采样（lag / mark / telemetry）+ 排空能力（lag 上限）。"""
    min_depth = _env_int("PROBEX_LIVE_SMOKE_MIN_DEPTH", 10)
    min_lag = _env_int("PROBEX_LIVE_SMOKE_MIN_LAG", 10)
    max_mark_age = _env_int("PROBEX_LIVE_SMOKE_MAX_MARK_AGE", 15_000)
    max_lag = _env_int("PROBEX_LIVE_SMOKE_MAX_LAG_MS", 30_000)
    lag = report["event_lag_ms"]
    mark = report["mark_age_ms"]
    case.assertGreaterEqual(report["smoke_duration_s"], window_seconds * 0.8, report)
    case.assertGreaterEqual(report["counts"]["depth_messages"], min_depth, report)
    case.assertIsNotNone(lag, report)
    case.assertGreaterEqual(lag["samples"], min_lag, report)
    case.assertIsNotNone(report["health"]["first_healthy_ms"], report)
    case.assertGreaterEqual(report["health"]["healthy_states"], 1, report)
    case.assertIsNotNone(mark, report)
    case.assertLessEqual(mark["max"], max_mark_age, report)
    case.assertLessEqual(lag["max"], max_lag, report)
    case.assertEqual(report["faults"]["malformed"], 0, report)


def _distribution(samples: list[int]) -> dict[str, int] | None:
    """min / median / p95 / max（样本不足时 None）。"""
    if not samples:
        return None
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "samples": len(ordered),
        "min": ordered[0],
        "median": int(statistics.median(ordered)),
        "p95": ordered[index],
        "max": ordered[-1],
    }


if __name__ == "__main__":
    unittest.main()
