"""Operational posture (closure Slice 5 / F-12 / F-15).

四层健康**必须互相独立**（不能互相替代）：

    PROCESS_LIVE        服务进程是否活着（不依赖交易所 / 行情 / provider）
    RUNTIME_RUNNING     已有 RuntimeStatusTracker 的会话状态
    TRADE_READINESS     ReadinessAuthority / Gate 的结果（可以 fail-closed）
    EXECUTION_HEALTH    P0001.13 的执行子系统健康

另有 network / auth / logging / retention 的只读姿态。本模块只组合事实，不做业务判断。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from market.events.types import Milliseconds

from storage.retention import RetentionPolicy

PROCESS_LIVE = "PROCESS_LIVE"
NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True, slots=True)
class NetworkPosture:
    bind_host: str
    loopback: bool
    allow_non_loopback: bool
    auth_required: bool
    auth_token_ref: str | None = None
    scheme: str = "bearer"

    def to_payload(self) -> dict[str, object]:
        return {"bind_host": self.bind_host, "loopback": self.loopback,
                "allow_non_loopback": self.allow_non_loopback, "auth_required": self.auth_required,
                "auth_token_ref": self.auth_token_ref, "scheme": self.scheme}


@dataclass(frozen=True, slots=True)
class HealthSplit:
    process_live: bool
    runtime_state: str
    runtime_detail: str
    trade_readiness: str
    trade_readiness_reasons: tuple[str, ...] = ()
    execution_health: str = "UNKNOWN"
    operational_warning: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {"process_live": self.process_live, "runtime_state": self.runtime_state,
                "runtime_detail": self.runtime_detail, "trade_readiness": self.trade_readiness,
                "trade_readiness_reasons": list(self.trade_readiness_reasons),
                "execution_health": self.execution_health,
                "operational_warning": self.operational_warning}


def build_health_split(*, runtime_state: str, runtime_detail: str,
                       readiness_status: str | None = None,
                       readiness_reasons: tuple[str, ...] = (),
                       execution_health: str | None = None) -> HealthSplit:
    """组合四层健康；**process_live 恒为 True**（本函数只在服务进程内被调用）。"""
    trade = str(readiness_status) if readiness_status else NOT_EVALUATED
    reasons = tuple(str(reason) for reason in readiness_reasons)
    health = str(execution_health) if execution_health else "UNKNOWN"
    warning: str | None = None
    if trade.upper() != "READY":   # readiness status 值大小写不固定（ready / READY）
        warning = f"process live but trade readiness is {trade}"
    return HealthSplit(process_live=True, runtime_state=str(runtime_state),
                       runtime_detail=str(runtime_detail), trade_readiness=trade,
                       trade_readiness_reasons=reasons, execution_health=health,
                       operational_warning=warning)


def build_retention_posture(*, policy: RetentionPolicy, runs_total: int, index_bytes: int,
                            record_bytes: int, event_store_bytes: int | None,
                            audit_entries: int, audit_capacity: int, latency_samples: int,
                            latency_capacity: int, logging_bounded: bool,
                            last_prune: Mapping[str, object] | None = None) -> dict[str, object]:
    """retention 姿态（bounded/unbounded + 当前 count/size + active policy + last prune）。"""
    return {
        "policy": policy.to_payload(),
        "bounded": not policy.is_unbounded,
        "stores": {
            "run_registry": {
                "bounded": policy.run_max_runs is not None or policy.run_max_age_ms is not None,
                "runs": int(runs_total), "index_bytes": int(index_bytes),
                "record_bytes": int(record_bytes)},
            "event_store": {
                "bounded": policy.event_store_max_bytes is not None,
                "bytes": (None if event_store_bytes is None else int(event_store_bytes)),
                "applied": False,
                "note": "event store is never auto-deleted/rotated (active replay source); "
                        "archival is out of scope"},
            "action_audit": {"bounded": True, "entries": int(audit_entries),
                             "capacity": int(audit_capacity)},
            "latency_samples": {"bounded": True, "samples": int(latency_samples),
                                "capacity": int(latency_capacity)},
            "logs": {"bounded": bool(logging_bounded),
                     "note": "stdout/stderr logs are not retained by the process"},
        },
        "last_prune": dict(last_prune) if last_prune is not None else None,
    }


def build_ops_payload(*, process_started_at_ms: int | None, network: NetworkPosture,
                      health: HealthSplit, logging: Mapping[str, object],
                      retention: Mapping[str, object], now_ms: Milliseconds) -> dict[str, object]:
    """完整 ops payload（供 snapshot / API / CLI / UI / Assistant 共用）。"""
    return {
        "process_live": health.process_live,
        "process_started_at_ms": process_started_at_ms,
        "runtime_state": health.runtime_state,
        "runtime_detail": health.runtime_detail,
        "trade_readiness": health.trade_readiness,
        "trade_readiness_reasons": list(health.trade_readiness_reasons),
        "execution_health": health.execution_health,
        "operational_warning": health.operational_warning,
        "network": network.to_payload(),
        "logging": dict(logging),
        "retention": dict(retention),
        "ts": int(now_ms),
    }


__all__ = ["HealthSplit", "NetworkPosture", "NOT_EVALUATED", "PROCESS_LIVE", "build_health_split",
           "build_ops_payload", "build_retention_posture"]
