"""P0001.9.4.1.1 SC-1 – SC-11：完整 Testnet 集成验证（public + private + recovery + income + readiness）。

⚠️ 本文件**只读**：它自身不下单、不撤单。生产代码同样没有下单能力（SC-12）。
需要在窗口内制造测试网业务事件时，由**仓库外** harness（`/tmp/probex_live/min_order_activity.py`）
在授权边界内执行（仅 Testnet / 仅 BTCUSDT / notional ≤ 100 USDT / 最终 flat + 0 挂单）。

```bash
export BINANCE_API_KEY=... BINANCE_API_SECRET=... PROBEX_LIVE_PRIVATE=1
export PROBEX_LIVE_MARKET_WINDOW_S=45          # public 观察窗口（秒）
export PROBEX_LIVE_MARKET_MAX_MARK_AGE_MS=5000
export PROBEX_LIVE_MARKET_MAX_FEED_AGE_MS=5000
export PROBEX_LIVE_READINESS_MAX_CLOCK_UNCERTAINTY_MS=300
export PROBEX_LIVE_READINESS_MAX_MEDIAN_LAG_MS=2000
export PROBEX_LIVE_READINESS_MAX_CALIBRATION_AGE_MS=600000
export PROBEX_LIVE_READINESS_MAX_AVAILABLE_BALANCE_AGE_MS=30000
export PROBEX_LIVE_INCOME_MAX_PAGES=20
python3 -m unittest -v tests.live.test_binance_full_readiness_live
```
"""

from __future__ import annotations

import json
import os
import time
import unittest

from connectors.binance.market_data.runtime import LiveMarketDataConfig, LiveMarketDataRuntime
from connectors.binance.market_data.snapshot import UrllibJsonClient
from connectors.binance.market_data.transport import ReconnectPolicy, connect
from connectors.binance.private.income import MAX_PAGE_LIMIT, fetch_income_history
from connectors.binance.private.recovery import RecoveryStatus
from readiness import (
    Environment,
    LiveReadinessGate,
    LiveReadinessStatus,
    historical_risk_baseline_from_income,
)
from risk.snapshot import utc_day_start_ms
from tests.live.market_readiness import (
    MarketReadinessPolicy,
    collect_market_evidence,
    market_evidence_report,
    market_ready,
    wait_for_healthy,
    window_start,
)
from tests.live.test_binance_readiness_live import (
    build_evidence,
    build_live_recovery,
    live_risk_policy_from_env,
    readiness_policy_from_env,
    stream_state_of,
)
from tests.live_support import BINANCE_SYMBOL

LIVE_FLAG = "PROBEX_LIVE_PRIVATE"
WINDOW_SECONDS_ENV = "PROBEX_LIVE_MARKET_WINDOW_S"
MAX_MARK_AGE_ENV = "PROBEX_LIVE_MARKET_MAX_MARK_AGE_MS"
MAX_FEED_AGE_ENV = "PROBEX_LIVE_MARKET_MAX_FEED_AGE_MS"


def market_policy_from_env() -> MarketReadinessPolicy:
    """public 判定阈值必须显式给出（产品 readiness policy 目前不含这些字段）。"""
    missing = [name for name in (MAX_MARK_AGE_ENV, MAX_FEED_AGE_ENV) if os.environ.get(name) in (None, "")]
    if missing:
        raise RuntimeError("market readiness thresholds must be provided explicitly: " + ", ".join(missing))
    return MarketReadinessPolicy(
        max_mark_age_ms=int(os.environ[MAX_MARK_AGE_ENV]),
        max_feed_age_ms=int(os.environ[MAX_FEED_AGE_ENV]),
    )


def build_public_runtime(*, rest_base: str, ws_host: str) -> LiveMarketDataRuntime:
    """真实 public market runtime（Testnet 端点）。"""
    return LiveMarketDataRuntime(
        config=LiveMarketDataConfig(
            symbol=BINANCE_SYMBOL,
            depth_speed="100ms",
            mark_price_speed="1s",
            depth_limit=100,
            connect_timeout_s=10.0,
            read_timeout_s=2.0,
            snapshot_timeout_s=10.0,
            exchange_info_timeout_s=10.0,
            reconnect=ReconnectPolicy(max_attempts=5, base_backoff_ms=500, max_backoff_ms=5_000),
            resync_cooldown_ms=1_000,
            history_limit=64,
            max_book_age_ms=5_000,
            ws_host=ws_host,
        ),
        http_client=UrllibJsonClient(base_url=rest_base),
        transport_factory=lambda url, timeout_s: connect(url, timeout_s=timeout_s),
    )


def pump_market_window(runtime: LiveMarketDataRuntime, *, window_seconds: float) -> int:
    """在真实窗口内持续 pump（返回累计处理的**市场事件**数）。"""
    deadline = time.time() + window_seconds
    pumped = 0
    while time.time() < deadline:
        batch = runtime.pump_once(timeout_s=0.5, max_messages=32)
        pumped += len(batch.market_events)
    return pumped


def run_full_chain(*, window_seconds: float, market_policy: MarketReadinessPolicy) -> tuple[dict, dict]:
    """完整链路：public → private → recovery → income → readiness（返回 report, facts）。"""
    from tests.live.test_binance_recovery_live import rest_base, ws_host

    report: dict = {"environment": {"rest_base": rest_base(), "ws_host": ws_host(), "symbol": BINANCE_SYMBOL}}

    public = build_public_runtime(rest_base=rest_base(), ws_host=ws_host())
    baseline = None
    try:
        public.load_trading_rules()
        public.measure_server_time_offset()
        public.connect()
        # 测量纪律：等到盘口首次 HEALTHY（初始快照/锚定完成）再开始计窗口，
        # 否则会把"启动对齐"误报成"深度连续性问题"。
        healthy = wait_for_healthy(public, timeout_s=10.0)
        baseline = window_start(public)
        pump_market_window(public, window_seconds=window_seconds)
        now_ms = int(time.time() * 1000)
        evidence = collect_market_evidence(public, baseline=baseline, now_ms=now_ms)
        ready, problems = market_ready(evidence, policy=market_policy)
        report["public_market"] = market_evidence_report(evidence, ready=ready, problems=problems)
        report["public_market"]["healthy_after_anchor"] = healthy
    finally:
        public.close()

    runtime, recovery, _tracker, accounting = build_live_recovery()
    try:
        runtime.start()
        runtime.refresh_clock_calibration()
        recovery_result = recovery.run(
            stream_state=stream_state_of(runtime), snapshot_provider=recovery.fetch_snapshot
        )
        report["recovery"] = {
            "status": recovery_result.status.value,
            "reasons": [reason.value for reason in recovery_result.reasons],
        }

        now_ms = int(time.time() * 1000)
        day_start = utc_day_start_ms(now_ms)
        income_facts = fetch_income_history(
            runtime.rest,
            window_start_ms=day_start,
            cutoff_ms=now_ms,
            page_limit=int(os.environ.get("PROBEX_LIVE_INCOME_PAGE_LIMIT", str(MAX_PAGE_LIMIT))),
            max_pages=int(os.environ["PROBEX_LIVE_INCOME_MAX_PAGES"]),
        )
        historic = historical_risk_baseline_from_income(
            income_facts, day_start_ts=day_start, cutoff_ts=now_ms
        )
        report["income"] = {
            "coverage_complete": income_facts.coverage.complete,
            "pages": income_facts.coverage.pages,
            "rows": income_facts.coverage.rows,
            "daily_net_realized": income_facts.trading_net_realized,
            "trading_rows": len(income_facts.trading_rows),
            "non_trading_rows": len(income_facts.non_trading_rows),
            "unclassified": [row.income_type for row in income_facts.unclassified_rows],
        }

        readiness_evidence, readiness_facts = build_evidence(
            runtime=runtime,
            recovery_status=recovery.state,
            accounting=accounting,
            now_ms=now_ms,
            day_start_ts=day_start,
            environment=Environment.TESTNET,
            market_ready=bool(report["public_market"]["ready"]),  # SC-2：来自真实 public 事实
            risk_policy=live_risk_policy_from_env(),
            historical_baseline=historic,
        )
        result = LiveReadinessGate(policy=readiness_policy_from_env()).evaluate(readiness_evidence)
        report["private_stream"] = {
            "listen_key_state": runtime.telemetry.listen_key_state,
            "continuity_assumed": runtime.telemetry.continuity_assumed,
            "boundary_present": runtime.snapshot_boundary is not None,
        }
        report["clock"] = None
        if runtime.clock_calibration is not None:
            calibration = runtime.clock_calibration
            report["clock"] = {
                "offset_ms": calibration.offset_ms,
                "round_trip_ms": calibration.round_trip_ms,
                "uncertainty_ms": calibration.uncertainty_ms,
                "calibration_age_ms": calibration.age_ms(now_ms=int(time.time() * 1000)),
            }
        distribution = runtime.telemetry.private_lag_ms
        raw_distribution = runtime.telemetry.raw_private_lag_ms
        last_raw = runtime.telemetry.last_raw_receive_lag_ms
        last_corrected = runtime.telemetry.last_receive_lag_ms
        report["private_latency"] = {
            "corrected_samples": None if distribution is None else distribution.samples,
            "corrected_min_ms": None if distribution is None else distribution.minimum,
            "corrected_median_ms": None if distribution is None else distribution.median,
            "corrected_max_ms": None if distribution is None else distribution.maximum,
            "raw_samples": None if raw_distribution is None else raw_distribution.samples,
            "raw_min_ms": None if raw_distribution is None else raw_distribution.minimum,
            "raw_median_ms": None if raw_distribution is None else raw_distribution.median,
            "raw_max_ms": None if raw_distribution is None else raw_distribution.maximum,
            "raw_last_ms": last_raw,
            "corrected_last_ms": last_corrected,
            "last_corrected_minus_raw_ms": (
                None if last_raw is None or last_corrected is None else last_corrected - last_raw
            ),
            "uncorrected_samples": runtime.telemetry.uncorrected_lag_sample_count,
        }
        report["readiness"] = {
            "status": result.status.value,
            "scope": None if result.scope is None else result.scope.value,
            "reasons": [reason.value for reason in result.reasons],
            "details": list(result.details),
        }
        report["facts"] = readiness_facts
    finally:
        runtime.stop()

    facts = {
        "recovery_status": report["recovery"]["status"],
        "market_ready": report["public_market"]["ready"],
        "public_market": report["public_market"],
        "income": report["income"],
        "clock": report["clock"],
        "private_latency": report["private_latency"],
        "readiness": report["readiness"],
        "historical_risk": report["facts"]["historical_risk"],
    }
    return report, facts


@unittest.skipUnless(
    os.environ.get(LIVE_FLAG) == "1", f"set {LIVE_FLAG}=1 to run the full testnet integration validation"
)
class FullReadinessIntegrationLiveTest(unittest.TestCase):
    def test_sc1_to_sc11_full_chain(self) -> None:
        window_seconds = float(os.environ.get(WINDOW_SECONDS_ENV, "45"))
        market_policy = market_policy_from_env()

        report, facts = run_full_chain(window_seconds=window_seconds, market_policy=market_policy)
        print("\nPROBEX FULL READINESS INTEGRATION REPORT (TESTNET):\n" + json.dumps(report, indent=2, ensure_ascii=False))

        # SC-1 / SC-2：public 真实运行，market_ready 由事实推导（不是硬编码）
        self.assertGreater(facts["public_market"]["snapshot_total"], 0, report)
        self.assertTrue(facts["public_market"]["anchor_established"], report)
        self.assertGreater(facts["public_market"]["state_count"], 0, report)
        self.assertGreater(facts["public_market"]["agg_trade_count"], 0, report)
        self.assertIn(facts["public_market"]["book_health"], {"healthy", "awaiting_snapshot", "resyncing", "stale"})

        # SC-7：income coverage complete + daily PnL known
        self.assertTrue(facts["income"]["coverage_complete"], report)
        self.assertEqual(facts["income"]["unclassified"], [], report)
        self.assertTrue(facts["historical_risk"]["daily_pnl_known"], report)

        # SC-8：activity（若由仓库外 harness 产生）之后 recovery 必须重新达成 RECOVERED
        self.assertEqual(facts["recovery_status"], RecoveryStatus.RECOVERED.value, report)

        # SC-10 / SC-11：完整 blocker 集合必须如实输出；本阶段不修 drawdown
        self.assertIn(facts["readiness"]["status"], {LiveReadinessStatus.LIVE_READY.value, LiveReadinessStatus.BLOCKED.value})
        if facts["readiness"]["status"] == LiveReadinessStatus.BLOCKED.value:
            self.assertTrue(facts["readiness"]["reasons"], report)
        self.assertFalse(facts["historical_risk"]["drawdown_known"], "drawdown must stay unknown (out of scope)")
        self.assertFalse(facts["historical_risk"]["peak_equity_known"])

    def test_sc5_corrected_equals_raw_plus_offset_when_samples_exist(self) -> None:
        """SC-4 / SC-5：真实样本里 `corrected = raw + offset`（无样本时如实报告 unknown）。

        两个精确不变量（同一批样本、同一 offset）：

        1. 最近一次样本：`last_corrected - last_raw == offset`；
        2. 分布形状：`corrected.min/median/max - raw.min/median/max == offset`。
        """
        window_seconds = float(os.environ.get(WINDOW_SECONDS_ENV, "45"))
        report, facts = run_full_chain(window_seconds=window_seconds, market_policy=market_policy_from_env())

        clock = facts["clock"]
        latency = facts["private_latency"]
        self.assertIsNotNone(clock, report)
        if latency["raw_last_ms"] is None or latency["corrected_samples"] in (None, 0):
            self.assertIsNone(latency["corrected_samples"], report)  # 无样本 ⇒ 未知（不是 0）
            self.skipTest("no private business event in window; corrected latency unknown (honest)")

        self.assertEqual(latency["uncorrected_samples"], 0, report)
        self.assertEqual(latency["corrected_samples"], latency["raw_samples"], report)
        self.assertEqual(latency["last_corrected_minus_raw_ms"], clock["offset_ms"], report)
        for name in ("min", "median", "max"):
            self.assertEqual(
                latency[f"corrected_{name}_ms"] - latency[f"raw_{name}_ms"],
                clock["offset_ms"],
                f"distribution invariant violated for {name}",
            )


if __name__ == "__main__":
    unittest.main()
