"""P0017.3 回测结果产物：manifest.json / summary.json / report.md。

只读派生（全部来自权威 accounting/tracker/trace 与输入 manifest）；
失败运行也产出诊断 summary，但状态非 SUCCESS，不被成功回测统计纳入。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from backtest.driver import BacktestResult
from backtest.identity import (
    BACKTEST_SCHEMA,
    EXECUTION_MODEL,
    canonical_json,
    sha256_text,
)

#: 第一版未建模/未支持的成本与执行能力（诚实声明，非零值）
SLIPPAGE_MODEL = "UNSUPPORTED"
FUNDING_MODEL = "UNSUPPORTED"
MARKET_IMPACT = "UNSUPPORTED"
QUEUE_POSITION = "MODELLED_BY_SIMULATED_VENUE (event-driven queue approximation, not validated against real order book)"


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                               timeout=5, check=False)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def build_manifest(result: BacktestResult, *, dataset_files: list[str],
                    input_files: list[dict]) -> dict:
    """生成不可变运行 manifest（设计 §9.1）。"""
    return {
        "manifest_schema": BACKTEST_SCHEMA,
        "execution_model": EXECUTION_MODEL,
        "run_id": result.run_id,
        "git_commit": _git_commit(),
        "dataset": {
            "sha256": result.dataset_sha256,
            "files": dataset_files,
        },
        "config": result.config.canonical(),
        "seed": result.config.seed,
        "execution_granularity": "event-driven",
        "cost_models": {
            "maker_fee": {"enabled": True, "maker_fee_rate": result.config.maker_fee_rate,
                            "fee_asset": result.config.fee_asset},
            "slippage": SLIPPAGE_MODEL,
            "funding": FUNDING_MODEL,
            "market_impact": MARKET_IMPACT,
            "queue_position": QUEUE_POSITION,
        },
        "portfolio_budget_enabled": result.config.max_portfolio_gross_notional is not None,
        "final_status": result.status.value,
        "integrity_checks": result.integrity_checks,
        "failure": ({"reason": result.reason, "location": result.location}
                     if not result.ok else None),
    }


def build_summary(result: BacktestResult) -> dict:
    """机器可读结果（设计 §11）。指标不可得时为 None，不填零。"""
    accounting = result.accounting
    equity = accounting.equity()
    realized = accounting.realized_trade_pnl
    fees = accounting.trading_fees
    net_realized = accounting.net_realized
    gross_exposure = accounting.gross_exposure
    positions = [
        {"symbol": p.symbol, "qty": p.qty, "avg_entry_price": p.avg_entry_price,
         "mark_price": p.mark_price, "notional": p.notional, "unrealized_pnl": p.unrealized_pnl}
        for p in accounting.positions
    ]
    statuses: dict[str, int] = {}
    for order in result.tracker.orders:
        statuses[order.status.value] = statuses.get(order.status.value, 0) + 1
    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "dataset_sha256": result.dataset_sha256,
        "trace": asdict(result.trace),
        "portfolio": {
            "initial_balance": result.config.initial_balance,
            "final_equity": equity,
            "balance": accounting.balance,
            "realized_trade_pnl": realized,
            "trading_fees": fees,
            "funding": accounting.funding.total,
            "net_realized": net_realized,
            "unrealized_pnl": accounting.unrealized_pnl(),
            "gross_exposure": gross_exposure,
            "net_exposure": accounting.net_exposure,
            "end_positions": positions,
        },
        "orders_by_status": statuses,
        "integrity_checks": result.integrity_checks,
        "capability_declarations": {
            "slippage_model": SLIPPAGE_MODEL,
            "funding_model": FUNDING_MODEL,
            "market_impact": MARKET_IMPACT,
            "execution_granularity": "event-driven (trades/order-book); bar-only = UNSUPPORTED",
            "limitations": ("Results are a simulation under the stated historical events, policy, "
                            "execution and cost assumptions; not evidence of live fills or future profit."),
        },
        "failure_reason": result.reason or None,
    }


def build_report_markdown(manifest: dict, summary: dict) -> str:
    p = summary["portfolio"]
    t = summary["trace"]
    lines = [
        f"# Backtest Report — {summary['run_id']}",
        "",
        f"- **Status:** {summary['status']}",
        f"- **Dataset sha256:** `{summary['dataset_sha256']}`",
        f"- **Git commit:** `{manifest.get('git_commit')}`",
        f"- **Execution:** {manifest['execution_granularity']} · {manifest['execution_model']}",
        f"- **Portfolio budget enabled:** {manifest['portfolio_budget_enabled']}",
        "",
        "## Trace",
        "",
        f"- events processed: {t['events']}",
        f"- decisions: {t['decisions']}",
        f"- submits: {t['submits']}",
        f"- risk rejects: {t['risk_rejects']}",
        f"- cancels: {t['cancels']}",
        f"- fills: {t['fills']}",
        "",
        "## Portfolio",
        "",
        f"- initial balance: {p['initial_balance']}",
        f"- final equity: {p['final_equity']}",
        f"- realized trade PnL: {p['realized_trade_pnl']}",
        f"- trading fees: {p['trading_fees']}",
        f"- funding: {p['funding']} ({FUNDING_MODEL})",
        f"- net realized: {p['net_realized']}",
        f"- unrealized PnL: {p['unrealized_pnl']}",
        f"- gross/net exposure: {p['gross_exposure']} / {p['net_exposure']}",
        f"- end positions: {len(p['end_positions'])}",
        "",
        "## Orders by status",
        "",
    ]
    for status, count in sorted(summary["orders_by_status"].items()):
        lines.append(f"- {status}: {count}")
    lines += ["", "## Integrity checks", ""]
    for check in summary["integrity_checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        lines.append(f"- [{mark}] {check['name']} {('— ' + check['detail']) if check['detail'] else ''}")
    cap = summary["capability_declarations"]
    lines += [
        "",
        "## Model boundaries / unsupported capabilities",
        "",
        f"- slippage: **{cap['slippage_model']}** (not zero; simply not modelled in v1)",
        f"- funding: **{cap['funding_model']}**",
        f"- market impact: **{cap['market_impact']}**",
        f"- execution: {cap['execution_granularity']}",
        f"- {cap['limitations']}",
        "",
        "## Risk rejects",
        "",
    ]
    rejects = t.get("rejects_log", [])
    if rejects:
        for item in rejects[:50]:
            lines.append(f"- ts={item['ts']} {item['reason']}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def write_artifacts(result: BacktestResult, *, out_dir: Path,
                     dataset_files: list[str]) -> dict[str, Path]:
    """写出 manifest/summary/report；返回路径映射。失败运行也写诊断产物。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    input_files = [{"path": p} for p in dataset_files]
    manifest = build_manifest(result, dataset_files=dataset_files, input_files=input_files)
    summary = build_summary(result)
    report = build_report_markdown(manifest, summary)
    paths = {
        "manifest": out_dir / "manifest.json",
        "summary": out_dir / "summary.json",
        "report": out_dir / "report.md",
    }
    paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                                   encoding="utf-8")
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
                                  encoding="utf-8")
    paths["report"].write_text(report, encoding="utf-8")
    # 规范化结果 hash（供双跑等价比较，不含临时路径/运行时间）
    result_hash = sha256_text(canonical_json(summary))
    paths["result_hash"] = result_hash  # type: ignore[assignment]
    return paths


__all__ = ["SLIPPAGE_MODEL", "build_manifest", "build_report_markdown", "build_summary",
           "write_artifacts"]
