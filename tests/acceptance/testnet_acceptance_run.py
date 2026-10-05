"""P0001.16 §14 最小 TESTNET 验收序列（A 挂单+撤单 / B 小额成交 / C 平仓）。

**这是验收工具，不是产品功能**：它只通过统一写路径执行
`Risk → Readiness → ExecutionEngine → PrivateExecutionConnector → Binance TESTNET`，
并使用 `runtime/testnet.py` 的生产组合（唯一写路径、唯一 Order/Accounting Owner）。

安全约束（D-034 + 人类裁决 2026-10-04）：

- 仅 TESTNET / 仅 BTCUSDT / notional ≤ 100 USDT；
- B（marketable 成交）需要 **显式** `PROBEX_TESTNET_ACCEPTANCE=1`（默认关闭）；
- A（挂单+撤单）使用正常 post-only（GTX）语义；
- 结束必须 `position = 0` 且 `open orders = 0`；无法清场 ⇒ 立即停止并报告（不继续其它写操作）；
- 凭据只从环境（`~/.probex/testnet.env` 由调用方注入）读取，从不打印、从不落盘。

用法：

```bash
set -a; . ~/.probex/testnet.env; set +a
export PROBEX_TESTNET_ACCEPTANCE=1          # 只有 B（真实成交）需要
python3 -m tests.acceptance.testnet_acceptance_run --confirm-testnet-writes
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

from execution.acceptance import ACCEPTANCE_FLAG_ENV, ACCEPTANCE_SYMBOL, testnet_acceptance_permission
from portfolio.types import Side
from readiness import LiveRiskPolicy, ReadinessPolicy
from readiness.bootstrap import BootstrapPhase
from readiness.types import PrivateLatencyStatus
from risk.high_watermark import ActivationPreconditions, HighWatermarkScope
from risk.types import OrderCorrelation, OrderProposal
from runtime.latency_observer import ExecutionLatencyObserver
from runtime.testnet import TestnetConfig, TestnetError, TestnetStack, build_testnet_stack

#: 验收规模（人类裁决：notional ≤ 100 USDT）
ACCEPTANCE_NOTIONAL_USDT = 50.0
#: 每个动作的观察窗口（秒）
WATCH_SECONDS = 12.0
#: 清场观察窗口（秒）
FLATTEN_WATCH_SECONDS = 25.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def env_flag(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value in (None, "") else value


def live_risk_policy() -> LiveRiskPolicy:
    """真实资金阶段必须显式配置的 risk policy（数值全部来自环境，不设产品默认值）。"""
    return LiveRiskPolicy(
        max_position_qty=float(env_flag("PROBEX_TESTNET_MAX_POSITION_QTY", "0.01")),
        max_order_notional=float(env_flag("PROBEX_TESTNET_MAX_ORDER_NOTIONAL", "100")),
        max_open_order_exposure=float(env_flag("PROBEX_TESTNET_MAX_OPEN_ORDER_EXPOSURE", "200")),
        max_daily_loss=float(env_flag("PROBEX_TESTNET_MAX_DAILY_LOSS", "200")),
        max_drawdown_pct=float(env_flag("PROBEX_TESTNET_MAX_DRAWDOWN_PCT", "0.5")),
        max_leverage=float(env_flag("PROBEX_TESTNET_MAX_LEVERAGE", "1")),
        max_mark_age_ms=int(env_flag("PROBEX_TESTNET_MAX_MARK_AGE_MS", "5000")))


@dataclass
class AcceptanceReport:
    report: dict[str, object] = field(default_factory=dict)

    def note(self, key: str, value: object) -> None:
        self.report[key] = value

    def dump(self) -> str:
        return json.dumps(self.report, ensure_ascii=False, indent=2, default=str)


def build_config(*, acceptance_enabled: bool) -> TestnetConfig:
    symbol = env_flag("PROBEX_TESTNET_SYMBOL", ACCEPTANCE_SYMBOL)
    from readiness import Environment as _Environment

    permission = testnet_acceptance_permission(
        environment=_Environment.TESTNET.value, symbol=symbol, enabled=acceptance_enabled,
        granted_by="human-session-2026-10-04")
    return TestnetConfig(symbol=symbol, risk_policy=live_risk_policy(),
                         readiness_policy=ReadinessPolicy(
                             max_clock_uncertainty_ms=int(env_flag("PROBEX_TESTNET_MAX_CLOCK_UNCERTAINTY_MS", "300")),
                             max_median_private_lag_ms=int(env_flag("PROBEX_TESTNET_MAX_MEDIAN_PRIVATE_LAG_MS", "2000")),
                             max_calibration_age_ms=int(env_flag("PROBEX_TESTNET_MAX_CALIBRATION_AGE_MS", "600000")),
                             max_available_balance_age_ms=int(env_flag("PROBEX_TESTNET_MAX_AVAILABLE_BALANCE_AGE_MS", "30000"))),
                         state_dir=os.path.expanduser(env_flag("PROBEX_TESTNET_STATE_DIR",
                                                              "~/.probex/runs/p0001.16")),
                         authority_ttl_ms=int(env_flag("PROBEX_TESTNET_AUTHORITY_TTL_MS", "60000")),
                         acceptance=permission,
                         market_window_s=float(env_flag("PROBEX_TESTNET_MARKET_WINDOW_S", "45")),
                         price_rounding=env_flag("PROBEX_TESTNET_PRICE_ROUNDING", "ROUND_DOWN"),
                         quantity_rounding=env_flag("PROBEX_TESTNET_QUANTITY_ROUNDING", "ROUND_DOWN"))


def price_for(side: Side, *, mark: float, tick: float, offset_ticks: int) -> float:
    """按 tick 生成限价（acceptance 自身的小算术，不使用策略逻辑）。"""
    direction = 1 if side is Side.BUY else -1
    return float(round((mark + direction * offset_ticks * tick) / tick) * tick)


def run(*, out_path: str | None = None) -> int:
    if os.environ.get("PROBEX_TESTNET_CONFIRM") != "1":
        raise SystemExit("refusing to place TESTNET orders: set PROBEX_TESTNET_CONFIRM=1 explicitly")
    acceptance_enabled = os.environ.get(ACCEPTANCE_FLAG_ENV) == "1"
    report = AcceptanceReport()
    config = build_config(acceptance_enabled=acceptance_enabled)
    report.note("mode", "TESTNET")
    report.note("symbol", config.symbol)
    report.note("acceptance_capability_enabled", config.acceptance is not None)
    report.note("acceptance_permission", None if config.acceptance is None else config.acceptance.view())

    stack = build_testnet_stack(config=config)
    latency = ExecutionLatencyObserver(log=__import__("runtime.latency_observer", fromlist=["BoundedLatencyLog"])
                                       .BoundedLatencyLog(), clock=_now_ms)
    stack.engine.latency_observer = lambda kind, ts: latency.note(kind, ts)
    notes: list[str] = []

    def cleanup_guard() -> None:
        """任何异常路径都必须先尝试撤单 + 清场，再报告。"""
        try:
            for order in stack.open_orders():
                stack.cancel(order.client_order_id)
            stack.pump_private(seconds=2.0)
        except Exception as exc:  # noqa: BLE001 - 清场失败必须可见
            notes.append(f"cleanup_failed:{type(exc).__name__}")

    try:
        stack.start()
        report.note("started", True)
        # 1) public 行情窗口 + market evidence（readiness 需要真实窗口，不接受"立刻 known"）
        healthy = stack.start_market_window(timeout_s=20.0)
        pumped = stack.pump_market(seconds=config.market_window_s)
        market_evidence = stack.collect_market_evidence()
        report.note("book_healthy_before_window", healthy)
        report.note("market_events", pumped)
        report.note("market_evidence", {"ready": market_evidence.ready,
                                        "problems": list(market_evidence.problems),
                                        "book_health": market_evidence.book_health,
                                        "anchored": market_evidence.anchored,
                                        "mark_age_ms": market_evidence.mark_age_ms,
                                        "feed_age_ms": market_evidence.feed_age_ms,
                                        "generation": market_evidence.generation})
        report.note("mark_price", stack.mark_price())

        # 2) private stream + startup recovery + readiness（既有规则，未修改）
        stack.pump_private(seconds=3.0)
        stack.refresh_account_facts()          # available-balance 新鲜度（readiness 既有要求）
        report.note("recovery", {"state": str(stack.recovery.state.value),
                                 "reasons": list(stack.recovery.reason_log_entries)}
                     if hasattr(stack.recovery, "reason_log_entries") else {"state": str(stack.recovery.state.value)})
        readiness = stack.collect_readiness()
        report.note("readiness", {"status": readiness.status.value,
                                  "reasons": [r.value for r in (getattr(readiness, "reasons", ()) or ())],
                                  "latency_status": getattr(getattr(readiness, "latency_status", None), "value", None)})

        # 3) HWM：未激活则按既有前置条件显式激活（不改规则，只调用既有机制）
        if str(stack.high_watermark.status.value) != "ACTIVE":
            snapshot = stack.risk_snapshot()
            collected = stack.last_collected_evidence
            historical = getattr(collected, "historical_risk", None)
            collected_daily_known = (bool(getattr(historical, "daily_pnl_known", False))
                                     if historical is not None
                                     else snapshot.realized_pnl_today is not None)
            preconditions = ActivationPreconditions(
                recovery_recovered=str(stack.recovery.state.value).upper() == "RECOVERED",
                position_flat=abs(stack.position_qty()) < 1e-9,
                open_orders_zero=len(stack.open_orders()) == 0,
                unresolved_orders_zero=stack.manager.unresolved_order_count == 0,
                daily_pnl_known=bool(collected_daily_known),
                current_equity_known=snapshot.equity is not None,
                account_snapshot_fresh=stack.private.latest_snapshot is not None,
                equity_consistent=True,
                scope=HighWatermarkScope.TESTNET,
                deployment_id=env_flag("PROBEX_TESTNET_DEPLOYMENT_ID", "p0001.16-acceptance"))
            try:
                stack.high_watermark.activate(preconditions=preconditions,
                                              current_equity=float(snapshot.equity or 0.0),
                                              activation_id=f"accept-{_now_ms()}",
                                              ts=_now_ms(), reason="P0001.16 acceptance bootstrap")
                report.note("high_watermark_activated", True)
            except Exception as exc:  # noqa: BLE001 - 激活失败 ⇒ 后面 readiness 会如实 BLOCKED
                report.note("high_watermark_activated", False)
                report.note("high_watermark_activation_error", f"{type(exc).__name__}: {exc}")
            readiness = stack.collect_readiness()
            report.note("readiness_after_hwm", {
                "status": readiness.status.value,
                "reasons": [r.value for r in (getattr(readiness, "reasons", ()) or ())]})

        # 4) 授权：LIVE_READY ⇒ NORMAL；只有 private latency UNOBSERVED ⇒ 既有冷启动路径（bootstrap）
        authority_kind = None
        try:
            authority = stack.issue_authority()
            authority_kind = "NORMAL"
            report.note("authority", {"kind": authority_kind,
                                      "authority_id": getattr(authority, "authority_id", None),
                                      "expires_at_ms": getattr(authority, "expires_at_ms", None)})
        except Exception as exc:  # noqa: BLE001
            activation = stack.activate_bootstrap(
                bootstrap_ttl_ms=int(env_flag("PROBEX_TESTNET_BOOTSTRAP_TTL_MS", "120000")),
                normal_ttl_ms=int(env_flag("PROBEX_TESTNET_AUTHORITY_TTL_MS", "60000")))
            report.note("authority", {"kind": "BOOTSTRAP" if activation.phase is BootstrapPhase.ACTIVE else "NONE",
                                      "phase": activation.phase.value,
                                      "reasons": list(activation.reasons),
                                      "normal_error": f"{type(exc).__name__}: {exc}"})
            if activation.phase is not BootstrapPhase.ACTIVE:
                report.note("blocked", "readiness does not permit TESTNET writes (not even cold start)")
                return _finish(stack, report, notes, out_path=out_path, exit_code=3)
            authority_kind = "BOOTSTRAP"

        if os.environ.get("PROBEX_TESTNET_PROBE_ONLY") == "1":
            report.note("probe_only", True)
            report.note("health", stack.health())
            report.note("mark_price", stack.mark_price())
            rules_ = stack.market.trading_rules
            report.note("trading_rules", None if rules_ is None else {
                "tick_size": rules_.tick_size, "step_size": rules_.step_size,
                "min_notional": rules_.min_notional, "status": rules_.status})
            report.note("position", stack.position_qty())
            report.note("open_orders", len(stack.open_orders()))
            return _finish(stack, report, notes, out_path=out_path, exit_code=0)

        stack.refresh_mark(seconds=3.0)          # 紧邻写之前刷新（避免 STALE_MARK；规则未改）
        rules = stack.market.trading_rules
        if rules is None:
            raise TestnetError("trading rules are unavailable; refusing to place orders")
        tick = float(rules.tick_size)
        mark = float(stack.mark_price() or 0.0)
        bid, ask = stack.book_touch()
        report.note("book_touch", {"best_bid": bid, "best_ask": ask})
        decision_id = f"acceptance-{_now_ms()}"
        correlation = OrderCorrelation(decision_id=decision_id, instrument_id="binance:BTCUSDT",
                                       venue_id="binance", prediction_id=None,
                                       market_state_hash=None)

        # ---------------- A. post-only 挂单 + 撤单（正常执行语义：GTX） ----------------
        # A 必须是**被动**挂单（GTX 若会成交会被交易所 -5022 拒绝）：低于 best bid 一个 margin
        reference_bid = float(bid if bid is not None else mark * 0.97)
        a_price = price_for(Side.BUY, mark=reference_bid, tick=tick, offset_ticks=-20)
        stack.refresh_mark(seconds=2.0)
        place = OrderProposal(symbol=config.symbol, side=Side.BUY,
                              quantity=float(env_flag("PROBEX_TESTNET_ACCEPTANCE_QTY", "0.001")),
                              price=a_price, post_only=True, reduce_only=False, correlation=correlation)
        latency.note_decision(_now_ms())
        placed = stack.submit(place)
        try:
            _classification = stack.adapter.last_submit_outcome.classification.value
        except Exception:  # noqa: BLE001
            _classification = "UNKNOWN"
        report.note("A_submit", {"classification": _classification, "submitted": placed.submitted,
                                 "rejected": placed.rejected,
                                 "unknown_submits": stack.adapter.unknown_submit_count,
                                 "adapter_last_submit": stack.adapter.last_submit_detail,
                                 "local_rejections": [(str(getattr(getattr(r, 'reason_code', None), 'value', None)),
                                                       str(getattr(r, 'details', ''))[:80])
                                                      for r in stack.engine.rejections][-3:],
                                 "reason_code": str(getattr(getattr(placed, "rejection", None), "reason_code", "")),
                                 "order": None if placed.order is None else placed.order.client_order_id,
                                 "time_in_force": "GTX"})
        a_order_id = None if placed.order is None else placed.order.client_order_id
        if a_order_id is not None:
            stack.pump_private(seconds=WATCH_SECONDS)
            order = stack.tracker.order(a_order_id)
            # UNKNOWN ⇒ 走唯一收敛入口（query 真实状态；不 retry submit、不伪造状态）
            if _classification == "UNKNOWN" or (order is not None and order.status.value == "PENDING_CREATE"):
                report.note("A_unknown_resolution", stack.resolve_unknown(a_order_id))
                order = stack.tracker.order(a_order_id)
            report.note("A_after_ack", {"status": None if order is None else order.status.value,
                                        "venue_order_id": None if order is None else order.exchange_order_id,
                                        "correlation_decision_id": None if order is None or order.correlation is None
                                        else order.correlation.decision_id})
            if order is not None and order.status.value in ("OPEN", "PARTIALLY_FILLED", "PENDING_CANCEL"):
                canceled = stack.cancel(a_order_id)
                report.note("A_cancel", {"requested": True, "updates": len(canceled.updates)})
                stack.pump_private(seconds=WATCH_SECONDS)
                order = stack.tracker.order(a_order_id)
                if order is not None and order.status.value == "PENDING_CANCEL":
                    # 撤单 ack 未到 ⇒ 只靠 query 收敛（不重发撤单）
                    report.note("A_cancel_resolution", stack.resolve_unknown(a_order_id))
                    order = stack.tracker.order(a_order_id)
                report.note("A_final", {"status": None if order is None else order.status.value})
            else:
                report.note("A_cancel", {"requested": False,
                                         "reason": "order is not confirmed live; UNKNOWN is preserved (no writes)"})
                report.note("A_final", {"status": None if order is None else order.status.value})

        # ---------------- B. 受控 marketable 成交（显式 acceptance capability；IOC） ----------------
        if config.acceptance is not None and mark > 0.0:
            stack.refresh_mark(seconds=2.0)
            mark = float(stack.mark_price() or mark)
            # 越过盘口：留足 margin，确保 ROUND_DOWN 归一化后仍然可成交（IOC）
            b_price = price_for(Side.BUY, mark=mark, tick=tick, offset_ticks=+30)
            fill_proposal = OrderProposal(symbol=config.symbol, side=Side.BUY,
                                          quantity=float(env_flag("PROBEX_TESTNET_ACCEPTANCE_QTY", "0.001")),
                                          price=b_price, post_only=False, reduce_only=False,
                                          correlation=correlation)
            before = stack.position_qty()
            fills_before = stack.fills_recorded
            filled = stack.submit(fill_proposal)
            report.note("B_submit", {"classification": str(stack.adapter.last_submit_outcome.classification.value)
                                     if stack.adapter.last_submit_outcome else None,
                                     "adapter_detail": stack.adapter.last_submit_detail,
                                     "submitted": filled.submitted, "rejected": filled.rejected,
                                     "time_in_force": "IOC", "price": b_price,
                                     "order": None if filled.order is None else filled.order.client_order_id})
            stack.pump_private(seconds=WATCH_SECONDS)
            b_order_id = None if filled.order is None else filled.order.client_order_id
            if b_order_id is not None and stack.tracker.require_order(b_order_id).status.value == "PENDING_CREATE":
                report.note("B_unknown_resolution", stack.resolve_unknown(b_order_id))
            order = None if b_order_id is None else stack.tracker.order(b_order_id)
            report.note("B_after_stream", {
                "status": None if order is None else order.status.value,
                "filled_quantity": None if order is None else order.filled_quantity,
                "avg_fill_price": None if order is None else order.avg_fill_price,
                "position_before": before, "position_after": stack.position_qty(),
                "fills_recorded": stack.fills_recorded - fills_before,
                "fill_ledger_size": len(stack.ledger.fills)})
        else:
            report.note("B_submit", {"skipped": "acceptance capability disabled (PROBEX_TESTNET_ACCEPTANCE != 1)"})

        # ---------------- C. 平仓（若 B 产生仓位） ----------------
        position = stack.position_qty()
        if abs(position) > 1e-9:
            stack.refresh_mark(seconds=2.0)
            bid, ask = stack.book_touch()
            close_side = Side.SELL if position > 0 else Side.BUY
            close_reference = float(bid if close_side is Side.SELL and bid is not None
                                    else (ask if ask is not None else mark))
            close_price = round(close_reference * (0.98 if close_side is Side.SELL else 1.02), 1)
            close_proposal = OrderProposal(symbol=config.symbol, side=close_side, quantity=abs(position),
                                           price=close_price, post_only=False, reduce_only=True,
                                           correlation=correlation)
            closed = stack.submit(close_proposal)
            report.note("C_flatten_submit", {"classification": str(stack.adapter.last_submit_outcome.classification.value)
                                             if stack.adapter.last_submit_outcome else None,
                                             "submitted": closed.submitted, "rejected": closed.rejected,
                                             "side": close_side.value, "quantity": abs(position),
                                             "reduce_only": True})
            stack.pump_private(seconds=FLATTEN_WATCH_SECONDS)
            report.note("C_after_flatten", {"position": stack.position_qty(),
                                            "open_orders": len(stack.open_orders())})

        report.note("health", stack.health())
        report.note("reconciliation", stack.reconciliation())
        report.note("latency_samples", dict(latency.log.samples) if hasattr(latency.log, "samples") else {})
        report.note("reconciliation_evidence", list(stack.reconciliation_evidence()))
        report.note("correlation", {"decision_id": decision_id,
                                    "orders": [order.client_order_id for order in stack.orders()],
                                    "linked": [order.client_order_id for order in stack.orders()
                                               if order.correlation is not None
                                               and order.correlation.decision_id == decision_id]})
        final_position = stack.position_qty()
        final_open = len(stack.open_orders())
        report.note("final", {"position": final_position, "open_orders": final_open,
                              "flat": abs(final_position) < 1e-9 and final_open == 0})
        cleanup_guard()
        final_position = stack.position_qty()
        final_open = len(stack.open_orders())
        report.note("final_after_cleanup", {"position": final_position, "open_orders": final_open,
                                            "flat": abs(final_position) < 1e-9 and final_open == 0})
        exit_code = 0
        if abs(final_position) > 1e-9 or final_open != 0:
            notes.append("RESIDUAL_STATE: position/open orders are not flat — stopping (no further writes)")
            exit_code = 4
        return _finish(stack, report, notes, out_path=out_path, exit_code=exit_code)
    except TestnetError as exc:
        notes.append(f"blocked:{exc}")
        cleanup_guard()
        return _finish(stack, report, notes, out_path=out_path, exit_code=3)
    except BaseException as exc:  # noqa: BLE001 - 任何异常都先尝试清场
        import traceback

        traceback.print_exc()
        notes.append(f"error:{type(exc).__name__}: {exc}")
        cleanup_guard()
        return _finish(stack, report, notes, out_path=out_path, exit_code=5)


def _finish(stack: TestnetStack, report: AcceptanceReport, notes: list[str], *, out_path: str | None,
            exit_code: int) -> int:
    report.note("notes", notes)
    try:
        stack.close()
        report.note("closed", True)
    except Exception as exc:  # noqa: BLE001
        report.note("closed", f"{type(exc).__name__}: {exc}")
    payload = report.dump()
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(payload, encoding="utf-8")
    print(payload)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out_path = None
    if "--out" in args:
        out_path = args[args.index("--out") + 1]
    return run(out_path=out_path)


if __name__ == "__main__":
    raise SystemExit(main())
