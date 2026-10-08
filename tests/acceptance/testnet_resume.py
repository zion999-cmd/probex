"""P0001.16 收尾 resumer（**有界**、可审计、无人值守时必须安全）。

用途：当账户存在残余真实仓位、而写授权被既有 readiness 规则阻塞时（NORMAL 需要已观测的私有流延迟；
BOOTSTRAP 需要 flat），等待**真实私有流事件**（例如资金费 `ACCOUNT_UPDATE`）出现，使延迟可观测，
随后在**一次运行**内完成：

1. 用 NORMAL authority 提交 **reduce-only IOC** 把残余仓位清零（不增加暴露）；
2. 确认 flat（position=0 / open_orders=0）；
3. 依次跑 §14 A（bootstrap 额度）/ B（acceptance IOC）/ C（reduce-only 清仓）；
4. 采集真实 latency 样本（submit_to_ack / cancel_to_ack / event_receive_lag / reconciliation_duration）；
5. 写出 JSON 报告。

安全约束：仅在 TESTNET / BTCUSDT / notional ≤ 100 USDT；所有写请求都经
`Risk → Readiness → ExecutionEngine → PrivateExecutionConnector → Binance`；不重试 UNKNOWN；
不伪造状态；每次动作前刷新 MARK；结束必须 flat；有界时间内未就绪则退出并如实报告。

用法（凭据从环境注入，不落盘）：
```bash
set -a; . ~/.probex/testnet.env; set +a
PROBEX_TESTNET_CONFIRM=1 PROBEX_TESTNET_ACCEPTANCE=1 \
python3 -m tests.acceptance.testnet_resume --max-wait-min 300 --out /tmp/probex_p116/resume.json
```
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio.types import Side
from readiness import LiveReadinessStatus
from risk.types import OrderCorrelation, OrderProposal
from runtime.testnet import TestnetConfig, TestnetError, build_testnet_stack
from tests.acceptance.testnet_acceptance_run import (build_config, env_flag, live_risk_policy)

MAX_REPORT_BYTES = 20_000


@dataclass
class ResumeReport:
    report: dict[str, object] = field(default_factory=dict)

    def note(self, key: str, value: object) -> None:
        self.report[key] = value
        self.flush()

    def flush(self) -> None:
        path = self.report.get("_out")
        if isinstance(path, str) and path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.report, ensure_ascii=False, indent=1, default=str)
            Path(path).write_text(payload[:MAX_REPORT_BYTES], encoding="utf-8")


def _stack(*, acceptance_enabled: bool):
    config = build_config(acceptance_enabled=acceptance_enabled)
    return config, build_testnet_stack(config=config)


def _flatten(stack, report: ResumeReport, *, attempts: int = 3, watch_s: float = 10.0) -> bool:
    """reduce-only IOC 清仓（仅 de-risking；不增加暴露）。返回是否 flat。"""
    for attempt in range(1, attempts + 1):
        position = stack.position_qty()
        if abs(position) < 1e-9:
            return True
        stack.refresh_mark(seconds=2.0)
        bid, ask = stack.book_touch()
        side = Side.SELL if position > 0 else Side.BUY
        reference = float(bid if (side is Side.SELL and bid) else (ask if ask else 0.0))
        price = round(reference * (0.98 if side is Side.SELL else 1.02), 1)
        proposal = OrderProposal(symbol=stack.config.symbol, side=side, quantity=abs(position),
                                 price=price, post_only=False, reduce_only=True,
                                 correlation=OrderCorrelation(decision_id=f"resume-flatten-{int(time.time())}",
                                                              instrument_id="binance:BTCUSDT",
                                                              venue_id="binance"))
        result = stack.submit(proposal)
        stack.pump_private(seconds=watch_s)
        classification = (str(stack.adapter.last_submit_outcome.classification.value)
                          if stack.adapter.last_submit_outcome else None)
        report.note(f"flatten_attempt_{attempt}", {
            "side": side.value, "quantity": abs(position), "price": price,
            "classification": classification, "audit": stack.adapter.last_submit_detail,
            "submitted": result.submitted, "rejected": result.rejected,
            "position_after": stack.position_qty(),
            "rejects": [str(getattr(getattr(r, "reason_code", None), "value", None))
                        for r in stack.engine.rejections][-3:]})
        if abs(stack.position_qty()) < 1e-9:
            return True
        time.sleep(1.0)
    return abs(stack.position_qty()) < 1e-9


def run(*, max_wait_min: int, out_path: str | None, poll_s: float = 90.0) -> int:
    if os.environ.get("PROBEX_TESTNET_CONFIRM") != "1":
        raise SystemExit("refusing to place TESTNET orders: set PROBEX_TESTNET_CONFIRM=1")
    acceptance_enabled = os.environ.get("PROBEX_TESTNET_ACCEPTANCE") == "1"
    report = ResumeReport()
    if out_path:
        report.report["_out"] = out_path
    report.note("started_at", int(time.time() * 1000))
    report.note("acceptance_enabled", acceptance_enabled)
    deadline = time.time() + max_wait_min * 60

    # ---------- 阶段 1：等待真实私有流事件使 private latency 可观测（不改规则、不写请求） ----------
    while True:
        config, stack = _stack(acceptance_enabled=acceptance_enabled)
        try:
            stack.start()
            # market evidence 需要**干净窗口**（出现 gap/resync 就换窗口；不改判定规则）
            market_ready = False
            for _attempt in range(3):
                stack.start_market_window(timeout_s=25)
                stack.pump_market(seconds=20)
                if stack.collect_market_evidence().ready:
                    market_ready = True
                    break
            stack.pump_private(seconds=20)
            stack.refresh_account_facts()
            stack.private.refresh_clock_calibration()      # 用**新鲜**校准（readiness 会检查新鲜度）
            readiness = stack.collect_readiness()
            position = stack.position_qty()
            calibration = stack.private.clock_calibration
            report.note("wait_state", {
                "at_ms": int(time.time() * 1000),
                "market_window_ready": market_ready,
                "clock": (None if calibration is None else
                          {"offset_ms": calibration.offset_ms, "rtt_ms": calibration.round_trip_ms,
                           "uncertainty_ms": calibration.uncertainty_ms}),
                "position": position,
                "open_orders": [o.client_order_id for o in stack.open_orders()],
                "readiness": readiness.status.value,
                "reasons": [r.value for r in (readiness.reasons or ())]})
            if readiness.status is LiveReadinessStatus.LIVE_READY:
                # ---------- 阶段 2：NORMAL authority ⇒ 清仓 → A/B/C ----------
                stack.issue_authority()
                report.note("authority", "NORMAL")
                flat = _flatten(stack, report)
                report.note("flat_after_close", flat)
                if not flat:
                    report.note("stopped", "cannot safely flatten residual position; no further writes")
                    return 4
                stack.close()
                break
            if time.time() >= deadline:
                report.note("stopped", f"no LIVE_READY within {max_wait_min} min; no write attempted")
                report.note("next_event_hint", "private stream needs a real account event (e.g. funding ACCOUNT_UPDATE)")
                return 3
        except TestnetError as exc:
            report.note("wait_error", f"TestnetError: {exc}")
        finally:
            try:
                stack.close()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(poll_s)

    # ---------- 阶段 3：A/B/C（统一路径；复用验收 driver 的序列） ----------
    from tests.acceptance.testnet_acceptance_run import run as run_acceptance

    report.note("flat_before_abc", True)
    code = run_acceptance(out_path=None if out_path is None else str(Path(out_path).with_suffix(".abc.json")))
    report.note("abc_exit_code", code)
    report.note("finished_at", int(time.time() * 1000))
    return code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out_path = args[args.index("--out") + 1] if "--out" in args else None
    max_wait_min = int(args[args.index("--max-wait-min") + 1]) if "--max-wait-min" in args else 300
    return run(max_wait_min=max_wait_min, out_path=out_path)


if __name__ == "__main__":
    raise SystemExit(main())
