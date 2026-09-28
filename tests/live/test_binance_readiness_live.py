"""P0001.9.4 SC-12 authenticated live readiness acceptance（opt-in，需要凭据）。

```bash
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
export PROBEX_LIVE_PRIVATE=1
# readiness 阈值必须**显式**给出（产品代码没有默认业务阈值）：
export PROBEX_LIVE_READINESS_MAX_CLOCK_UNCERTAINTY_MS=300
export PROBEX_LIVE_READINESS_MAX_MEDIAN_LAG_MS=2000
export PROBEX_LIVE_READINESS_MAX_CALIBRATION_AGE_MS=600000
export PROBEX_LIVE_READINESS_MAX_AVAILABLE_BALANCE_AGE_MS=30000
python3 -m unittest -v tests.live.test_binance_readiness_live
```

流程：真实 private runtime（listenKey → stream → snapshot → boundary）→ 真实时钟校准 →
真实 recovery → 用**真实事实**组装 `LiveReadinessEvidence` → `LiveReadinessGate` 判定。

安全：全程只读；凭据只从环境变量读取；报告不包含任何凭据/签名。
本测试**不会**下单或撤单（readiness 本身也没有这种能力）。
"""

from __future__ import annotations

import json
import os
import time
import unittest

from connectors.binance.private.recovery import RecoveryStatus
from market.events.types import Milliseconds
from portfolio.accounting import AccountingCore
from readiness import (
    Environment,
    LiveReadinessEvidence,
    LiveReadinessGate,
    LiveReadinessStatus,
    LiveRiskPolicy,
    ReadinessPolicy,
    account_evidence,
    environment_evidence,
    exchange_available_balance,
    historical_risk_evidence_from_snapshot,
    private_stream_evidence,
)
from risk.high_watermark import HighWatermarkEvidence
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from tests.live.test_binance_recovery_live import build_live_recovery, rest_base, ws_host
from tests.live_support import BINANCE_SYMBOL

LIVE_FLAG = "PROBEX_LIVE_PRIVATE"
POLICY_ENV = {
    "max_clock_uncertainty_ms": "PROBEX_LIVE_READINESS_MAX_CLOCK_UNCERTAINTY_MS",
    "max_median_private_lag_ms": "PROBEX_LIVE_READINESS_MAX_MEDIAN_LAG_MS",
    "max_calibration_age_ms": "PROBEX_LIVE_READINESS_MAX_CALIBRATION_AGE_MS",
    "max_available_balance_age_ms": "PROBEX_LIVE_READINESS_MAX_AVAILABLE_BALANCE_AGE_MS",
}


def readiness_policy_from_env() -> ReadinessPolicy:
    """从环境显式读取阈值；缺失即拒绝运行（**不猜业务数值**）。"""
    missing = [name for name in POLICY_ENV.values() if os.environ.get(name) in (None, "")]
    if missing:
        raise RuntimeError("readiness thresholds must be provided explicitly: " + ", ".join(missing))
    return ReadinessPolicy(**{key: int(os.environ[value]) for key, value in POLICY_ENV.items()})


def live_risk_policy_from_env() -> LiveRiskPolicy:
    """真实资金风险策略必须由人类配置；缺失即拒绝（不得用 None 当"安全"）。"""
    required = {
        "max_position_qty": "PROBEX_LIVE_RISK_MAX_POSITION_QTY",
        "max_order_notional": "PROBEX_LIVE_RISK_MAX_ORDER_NOTIONAL",
        "max_open_order_exposure": "PROBEX_LIVE_RISK_MAX_OPEN_ORDER_EXPOSURE",
        "max_daily_loss": "PROBEX_LIVE_RISK_MAX_DAILY_LOSS",
        "max_drawdown_pct": "PROBEX_LIVE_RISK_MAX_DRAWDOWN_PCT",
        "max_leverage": "PROBEX_LIVE_RISK_MAX_LEVERAGE",
        "max_mark_age_ms": "PROBEX_LIVE_RISK_MAX_MARK_AGE_MS",
    }
    missing = [value for value in required.values() if os.environ.get(value) in (None, "")]
    if missing:
        return None  # type: ignore[return-value]
    values: dict[str, object] = {
        key: float(os.environ[env_name]) for key, env_name in required.items()
    }
    values["max_mark_age_ms"] = int(os.environ[required["max_mark_age_ms"]])
    return LiveRiskPolicy(**values)  # type: ignore[arg-type]


def build_evidence(
    *,
    runtime,
    recovery_status: RecoveryStatus,
    accounting: AccountingCore,
    now_ms: Milliseconds,
    day_start_ts: Milliseconds,
    environment: Environment,
    market_ready: bool,
    risk_policy: LiveRiskPolicy | None,
    historical_baseline=None,
    high_watermark=None,
) -> tuple[LiveReadinessEvidence, dict]:
    """用真实对象组装证据并返回 `(evidence, facts)`；facts 供报告使用（无凭据）。"""
    observation = runtime.latest_snapshot
    if observation is None:
        raise RuntimeError("no account snapshot available; call runtime.refresh_snapshot() first")
    exchange_balance = exchange_available_balance(observation)
    snapshot = build_risk_snapshot(
        accounting,
        symbol=BINANCE_SYMBOL,
        now_ms=now_ms,
        day_start_ts=day_start_ts,
        exchange_available_balance=exchange_balance,
        # P0001.9.4.1：注入受信历史 baseline ⇒ 当日 PnL 可知
        historical_baseline=historical_baseline,
        # P0001.9.4.2：注入已确认 durable HWM ⇒ drawdown/peak 可知（未提供 ⇒ 保持 UNKNOWN）
        high_watermark=high_watermark,
    )
    evidence = LiveReadinessEvidence(
        now_ms=now_ms,
        recovery_status=recovery_status,
        private_stream=private_stream_evidence(
            runtime.telemetry, boundary_present=runtime.snapshot_boundary is not None
        ),
        account=account_evidence(observation),
        historical_risk=historical_risk_evidence_from_snapshot(snapshot),
        environment=environment_evidence(environment=environment),
        market_ready=market_ready,
        risk_policy=risk_policy,
        high_watermark=(
            HighWatermarkEvidence.uninitialized()
            if high_watermark is None
            else high_watermark
        ),
    )
    facts = {
        "account_can_trade": observation.can_trade,
        "exchange_available_balance": None if exchange_balance is None else exchange_balance.value,
        "available_balance_source": snapshot.available_balance_source.value,
        "available_balance_age_ms": snapshot.available_balance_age_ms,
        "risk_snapshot": {
            "balance": snapshot.balance,
            "equity": snapshot.equity,
            "peak_equity": snapshot.peak_equity,
            "drawdown": snapshot.drawdown,
            "drawdown_pct": snapshot.drawdown_pct,
            "realized_pnl_today": snapshot.realized_pnl_today,
            "position_qty": snapshot.position_qty,
        },
        "high_watermark": None
        if high_watermark is None
        else {
            "status": high_watermark.status.value,
            "peak_equity": high_watermark.peak_equity,
            "activation_id": high_watermark.activation_id,
            "activation_equity": high_watermark.activation_equity,
            "generation": high_watermark.generation,
            "detail": high_watermark.detail,
        },
        "historical_baseline": None
        if historical_baseline is None
        else {
            "source": historical_baseline.source,
            "day_start_ts": historical_baseline.day_start_ts,
            "cutoff_ts": historical_baseline.cutoff_ts,
            "daily_net_realized": historical_baseline.daily_net_realized,
            "trading_rows": historical_baseline.trading_rows,
            "non_trading_rows": historical_baseline.non_trading_rows,
            "coverage_complete": historical_baseline.coverage_complete,
            "detail": historical_baseline.detail,
        },
        "historical_risk": {
            "daily_pnl_known": evidence.historical_risk.daily_pnl_known,
            "drawdown_known": evidence.historical_risk.drawdown_known,
            "peak_equity_known": evidence.historical_risk.peak_equity_known,
        },
    }
    return evidence, facts


def build_readiness_report(
    *, policy: ReadinessPolicy, result, evidence: LiveReadinessEvidence, facts: dict, runtime,
    recovery_status: RecoveryStatus, accounting: AccountingCore, elapsed_ms: int,
) -> dict:
    calibration = runtime.clock_calibration
    distribution = runtime.telemetry.private_lag_ms
    return {
        "environment": {
            "rest_base": rest_base(),
            "ws_host": ws_host(),
            "symbol": BINANCE_SYMBOL,
            "chain": "TESTNET" if "demo" in rest_base() else "UNKNOWN",
            "note": "只读 readiness 评估；无下单/撤单",
        },
        "policy": {
            "max_clock_uncertainty_ms": policy.max_clock_uncertainty_ms,
            "max_median_private_lag_ms": policy.max_median_private_lag_ms,
            "max_calibration_age_ms": policy.max_calibration_age_ms,
            "max_available_balance_age_ms": policy.max_available_balance_age_ms,
            "source": "env（人类显式配置；产品代码无默认值）",
        },
        "recovery": {"status": recovery_status.value},
        "private_stream": {
            "listen_key_state": runtime.telemetry.listen_key_state,
            "continuity_assumed": runtime.telemetry.continuity_assumed,
            "boundary_present": runtime.snapshot_boundary is not None,
            "heartbeats": runtime.telemetry.heartbeat_count,
            "discontinuity_events": list(runtime.discontinuity_events),
        },
        "clock": None
        if calibration is None
        else {
            "offset_ms": calibration.offset_ms,
            "round_trip_ms": calibration.round_trip_ms,
            "uncertainty_ms": calibration.uncertainty_ms,
            "age_ms": calibration.age_ms(now_ms=int(time.time() * 1000)),
        },
        "latency": {
            "corrected_median_ms": None if distribution is None else distribution.median,
            "corrected_samples": None if distribution is None else distribution.samples,
            "raw_last_ms": runtime.telemetry.last_raw_receive_lag_ms,
            "uncorrected_samples": runtime.telemetry.uncorrected_lag_sample_count,
        },
        "readiness": {
            "status": result.status.value,
            "scope": None if result.scope is None else result.scope.value,
            "reasons": [reason.value for reason in result.reasons],
            "details": list(result.details),
            "elapsed_ms": elapsed_ms,
        },
        "facts": facts,
        "accounting": {
            "baseline_applied": accounting.baseline_applied,
            "historical_pnl_known": accounting.historical_pnl_known,
            "synthetic_fills": len(accounting.fills.fills),
        },
    }


@unittest.skipUnless(
    os.environ.get(LIVE_FLAG) == "1", f"set {LIVE_FLAG}=1 to run the authenticated live readiness acceptance"
)
class BinanceReadinessLiveTest(unittest.TestCase):
    def test_sc12_ready_or_explainably_blocked(self) -> None:
        policy = readiness_policy_from_env()
        risk_policy = live_risk_policy_from_env()
        runtime, recovery, _tracker, accounting = build_live_recovery()
        try:
            runtime.start()
            runtime.refresh_clock_calibration()  # 新鲜的不确定度（D-036）
            recovery_result = recovery.run(
                stream_state=stream_state_of(runtime), snapshot_provider=recovery.fetch_snapshot
            )
            now_ms = int(time.time() * 1000)
            started = int(time.time() * 1000)
            evidence, facts = build_evidence(
                runtime=runtime,
                recovery_status=recovery.state,
                accounting=accounting,
                now_ms=now_ms,
                day_start_ts=utc_day_start_ms(now_ms),
                environment=Environment.TESTNET,
                # 本 harness 不运行 public market-data 链（它有自己的验收 P0001.9.1）⇒ 不伪造 green
                market_ready=False,
                risk_policy=risk_policy,
            )
            result = LiveReadinessGate(policy=policy).evaluate(evidence)
            elapsed_ms = int(time.time() * 1000) - started
            report = build_readiness_report(
                policy=policy, result=result, evidence=evidence, facts=facts, runtime=runtime,
                recovery_status=recovery.state, accounting=accounting, elapsed_ms=elapsed_ms,
            )
            print("\nPROBEX LIVE READINESS REPORT (TESTNET):\n" + json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            runtime.stop()

        # SC-12：必须给出**明确**结果；BLOCKED 必须带原因码
        self.assertIn(result.status, {LiveReadinessStatus.LIVE_READY, LiveReadinessStatus.BLOCKED})
        if result.status is LiveReadinessStatus.LIVE_READY:
            self.assertEqual(result.reasons, ())
        else:
            self.assertTrue(result.reasons, "BLOCKED must carry at least one reason code")
        # 真实链路事实必须参与判定（RECOVERED 只是输入之一）
        self.assertIs(recovery_result.status, RecoveryStatus.RECOVERED)


def stream_state_of(runtime):
    """由真实 runtime 事实构造 StreamState（不伪造 ACTIVE / continuity）。"""
    from tests.live.test_binance_recovery_live import stream_state_from

    return stream_state_from(runtime)


if __name__ == "__main__":
    unittest.main()
