"""由既有事实构造 `RunSummary`（P0001.10.2 §4）。

纪律：

- **只汇总已记录的事实**（计数 / 求和 / 取值），不重新运行策略，也**不重新计算 Accounting**
  （fills / fees / pnl / exposure / position 一律由调用方作为事实传入）；
- 未知事实保持 UNKNOWN；
- 输出对同一组输入**确定性一致**（不引入 wall-clock 或随机量）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from market.events.types import Milliseconds

from product.types import Fact, RuntimeIdentity
from reports.types import RunIdentity, RunSummary


def _status_of(order: object) -> str:
    raw = getattr(order, "status", None)
    value = getattr(raw, "value", raw)
    return str(value) if value else "unknown"


def build_run_summary(
    *,
    identity: RuntimeIdentity,
    run_id: str,
    started_at: Milliseconds,
    telemetry: Sequence[object] = (),
    orders: Sequence[object] = (),
    fills: int | None = None,
    fees: float | None = None,
    realized_pnl: float | None = None,
    unrealized_pnl: float | None = None,
    max_exposure: float | None = None,
    final_position: float | None = None,
    risk_rejects: Sequence[str] = (),
    readiness_blocks: Sequence[str] = (),
    anomalies: Sequence[str] = (),
    config_fingerprints: Mapping[str, object] | None = None,
    ended_at: Milliseconds | None = None,
    data_range: tuple[int, int] | None = None,
    config_id: str | None = None,
    metrics_payload: dict[str, object] | None = None,
) -> RunSummary:
    """汇总一轮运行的事实（计数来自 telemetry 记录，不重算策略）。"""
    market_healthy_rounds = sum(1 for t in telemetry if bool(getattr(t, "market_healthy", False)))
    prediction_rounds = sum(1 for t in telemetry if getattr(t, "prediction_id", None) is not None)
    ages = [int(a) for a in (getattr(t, "prediction_age_ms", None) for t in telemetry)
            if isinstance(a, int)]
    authority_reasons = [str(getattr(t, "authority_reason", "") or "") for t in telemetry
                         if getattr(t, "authority_valid", True) is False]
    counts = {
        "loops": len(telemetry),
        "submit": sum(int(getattr(t, "submit_count", 0)) for t in telemetry),
        "cancel": sum(int(getattr(t, "cancel_count", 0)) for t in telemetry),
        "replace": sum(int(getattr(t, "replace_count", 0)) for t in telemetry),
        "risk_reject": sum(int(getattr(t, "risk_reject_count", 0)) for t in telemetry),
        "unknown_submit": sum(int(getattr(t, "unknown_submit_count", 0)) for t in telemetry),
    }
    statuses: dict[str, int] = {}
    for order in orders:
        status = _status_of(order)
        statuses[status] = statuses.get(status, 0) + 1
    loop_ts = [int(getattr(t, "loop_ts", 0) or 0) for t in telemetry]
    duration = max(0, max(loop_ts) - min(loop_ts)) if loop_ts else 0
    fingerprints = {str(k): Fact.of(v) for k, v in (config_fingerprints or {}).items()}
    return RunSummary(
        run=RunIdentity(
            run_id=run_id,
            runtime=identity,
            started_at=int(started_at),
            ended_at=(Fact.unknown("run has not ended") if ended_at is None else Fact.of(int(ended_at))),
            config_fingerprints=fingerprints,
            data_range=(Fact.unknown("data range not provided") if data_range is None
                        else Fact.of([int(data_range[0]), int(data_range[1])])),
            config_id=(Fact.unknown("no config snapshot bound") if config_id is None else Fact.of(config_id)),
        ),
        duration_ms=duration,
        market_health={"healthy_rounds": market_healthy_rounds,
                       "unhealthy_rounds": len(telemetry) - market_healthy_rounds,
                       "rounds": len(telemetry)},
        prediction={"rounds_with_prediction": prediction_rounds,
                    "median_age_ms": _median(ages),
                    "samples": len(ages)},
        decision_counts=counts,
        order_counts=statuses,
        fills=(Fact.unknown("fills not provided") if fills is None else Fact.of(fills)),
        fees=(Fact.unknown("fees not provided") if fees is None else Fact.of(fees)),
        realized_pnl=(Fact.unknown("realized pnl not provided") if realized_pnl is None
                      else Fact.of(realized_pnl)),
        unrealized_pnl=(Fact.unknown("unrealized pnl not provided") if unrealized_pnl is None
                        else Fact.of(unrealized_pnl)),
        max_exposure=(Fact.unknown("max exposure not provided") if max_exposure is None
                      else Fact.of(max_exposure)),
        final_position=(Fact.unknown("final position not provided") if final_position is None
                        else Fact.of(final_position)),
        readiness_blockers=tuple(str(r) for r in readiness_blocks),
        anomalies=tuple(str(a) for a in anomalies) + tuple(r for r in authority_reasons if r),
        metrics=dict(metrics_payload or {}),
    )


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
