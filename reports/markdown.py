"""Run Summary → Markdown（P0001.10.2 §4）：确定性文本，UNKNOWN 显式。"""

from __future__ import annotations

from product.types import Fact
from reports.types import RunSummary


def _fact(fact: Fact) -> str:
    if not fact.known:
        return f"UNKNOWN ({fact.reason})"
    return str(fact.value)


def _rows(pairs: list[tuple[str, str]]) -> str:
    lines = ["| field | value |", "| --- | --- |"]
    lines.extend(f"| {key} | {value} |" for key, value in pairs)
    return "\n".join(lines)


def summary_to_markdown(summary: RunSummary) -> str:
    """把 RunSummary 渲染为 Markdown（字段顺序固定 ⇒ 输出确定）。"""
    if not isinstance(summary, RunSummary):
        raise TypeError("summary_to_markdown requires a RunSummary")
    run = summary.run
    parts: list[str] = []
    parts.append(f"# Run Summary — {run.run_id}\n")
    parts.append("## Run identity\n")
    parts.append(_rows([
        ("run_id", run.run_id),
        ("runtime_id", run.runtime.runtime_id),
        ("mode", run.runtime.mode.value),
        ("environment", run.runtime.environment),
        ("venue", run.runtime.venue),
        ("symbol", run.runtime.symbol),
        ("started_at", str(run.started_at)),
        ("ended_at", _fact(run.ended_at)),
        ("duration_ms", str(summary.duration_ms)),
        ("data_range", _fact(run.data_range)),
    ]) + "\n")
    if run.config_fingerprints:
        parts.append("## Config fingerprints\n")
        parts.append(_rows([(k, _fact(v)) for k, v in sorted(run.config_fingerprints.items())]) + "\n")
    parts.append("## Market / prediction\n")
    parts.append(_rows([
        ("healthy_rounds", str(summary.market_health["healthy_rounds"])),
        ("unhealthy_rounds", str(summary.market_health["unhealthy_rounds"])),
        ("rounds_with_prediction", str(summary.prediction["rounds_with_prediction"])),
        ("median_prediction_age_ms",
         "UNKNOWN" if summary.prediction["median_age_ms"] is None else str(summary.prediction["median_age_ms"])),
    ]) + "\n")
    parts.append("## Decision counts\n")
    parts.append(_rows([(k, str(v)) for k, v in sorted(summary.decision_counts.items())]) + "\n")
    parts.append("## Orders\n")
    parts.append(_rows([(k, str(v)) for k, v in sorted(summary.order_counts.items())]
                       or [("orders", "none recorded")]) + "\n")
    parts.append("## Facts\n")
    parts.append(_rows([
        ("fills", _fact(summary.fills)),
        ("fees", _fact(summary.fees)),
        ("realized_pnl", _fact(summary.realized_pnl)),
        ("unrealized_pnl", _fact(summary.unrealized_pnl)),
        ("max_exposure", _fact(summary.max_exposure)),
        ("final_position", _fact(summary.final_position)),
    ]) + "\n")
    parts.append("## Blockers / anomalies\n")
    parts.append(_rows([
        ("risk_rejects", ", ".join(summary.anomalies) if False else "see anomalies"),
        ("readiness_blockers", ", ".join(summary.readiness_blockers) or "none"),
        ("anomalies", ", ".join(summary.anomalies) or "none"),
    ]) + "\n")
    return "\n".join(parts)
