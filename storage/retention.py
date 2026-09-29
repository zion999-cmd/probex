"""Retention / Growth policy (closure Slice 5 / F-15).

目标：让增长策略**明确、可配置、可观察** —— 不是"自动帮用户删东西"。

纪律：

- 所有 durable retention 参数**显式配置**；未配置 ⇒ `UNBOUNDED`（**绝不偷偷删除**）；
- 本模块不设置任何生产数值；数值由人类/操作者给出；
- 删除/轮转绝不触及：active run、active marker、HWM / durable risk state、当前 event source；
- 这不是 backup/archival（本 Slice 不建设 archival service）；
- 本模块不 import logging（保持分层）：调用方负责记录 retention 操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market.events.types import Milliseconds


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """显式 retention 配置；**全部 None ⇒ UNBOUNDED**（默认不删任何东西）。"""

    #: 保留的**已完成** run 上限（最新优先）
    run_max_runs: int | None = None
    #: 已完成 run 的最大年龄（ms）
    run_max_age_ms: int | None = None
    #: event store 的目标字节上限（**本阶段只报告，不自动删除**：当前 source 不得被移除）
    event_store_max_bytes: int | None = None
    #: 结构化日志的轮转大小 / 备份数（由 logging 配置消费）
    log_max_bytes: int | None = None
    log_backup_count: int | None = None

    def __post_init__(self) -> None:
        for name in ("run_max_runs", "run_max_age_ms", "event_store_max_bytes", "log_max_bytes",
                     "log_backup_count"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"RetentionPolicy.{name} must be a non-negative int or None")
        if self.run_max_runs == 0:
            raise ValueError("RetentionPolicy.run_max_runs=0 would delete every finished run; "
                             "refusing (use a positive value or None)")

    @property
    def is_unbounded(self) -> bool:
        return all(value is None for value in (self.run_max_runs, self.run_max_age_ms,
                                               self.event_store_max_bytes, self.log_max_bytes,
                                               self.log_backup_count))

    def to_payload(self) -> dict[str, object]:
        return {"run_max_runs": self.run_max_runs, "run_max_age_ms": self.run_max_age_ms,
                "event_store_max_bytes": self.event_store_max_bytes,
                "log_max_bytes": self.log_max_bytes, "log_backup_count": self.log_backup_count,
                "unbounded": self.is_unbounded}


@dataclass(frozen=True, slots=True)
class PruneReport:
    """一次 retention 操作的结果（可观察、可审计）。"""

    applied: bool
    reason: str
    policy: RetentionPolicy
    removed_run_ids: tuple[str, ...] = ()
    removed_record_files: int = 0
    kept_runs: int = 0
    skipped_active_run_id: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {"applied": self.applied, "reason": self.reason, "policy": self.policy.to_payload(),
                "removed_run_ids": list(self.removed_run_ids),
                "removed_record_files": self.removed_record_files, "kept_runs": self.kept_runs,
                "skipped_active_run_id": self.skipped_active_run_id}


def _ended_at(record: object) -> int | None:
    ended = getattr(record, "ended_at", None)
    if ended is not None and getattr(ended, "known", False):
        return int(ended.value)  # type: ignore[arg-type]
    return None


def prune_finished_runs(registry: object, policy: RetentionPolicy, *,
                        now_ms: Milliseconds) -> PruneReport:
    """按 policy 删除**已完成**的旧 run（record 文件 + 索引压缩）；active run 永不删除。

    只处理**有 finalize 事件**的 run；RUNNING / 崩溃残留（无 finalize）一律保留（不掩盖崩溃）。
    """
    active = registry.active_run_id()  # type: ignore[attr-defined]
    finalized = [record for record in registry.finalized_runs()  # type: ignore[attr-defined]
                 if record.run_id != active]
    if policy.run_max_runs is None and policy.run_max_age_ms is None:
        return PruneReport(applied=False, reason="retention policy is UNBOUNDED (nothing deleted)",
                           policy=policy, kept_runs=len(finalized),
                           skipped_active_run_id=active)
    doomed: set[str] = set()
    if policy.run_max_age_ms is not None:
        cutoff = int(now_ms) - int(policy.run_max_age_ms)
        for record in finalized:
            ended = _ended_at(record) or record.started_at
            if int(ended) < cutoff:
                doomed.add(record.run_id)
    if policy.run_max_runs is not None:
        remaining = [record for record in finalized if record.run_id not in doomed]
        remaining.sort(key=lambda record: (record.started_at, record.run_id))
        excess = len(remaining) - int(policy.run_max_runs)
        if excess > 0:
            doomed.update(record.run_id for record in remaining[:excess])
    if not doomed:
        return PruneReport(applied=True, reason="policy applied; nothing matched the prune rule",
                           policy=policy, kept_runs=len(finalized), skipped_active_run_id=active)
    removed_files = int(registry.remove_runs(sorted(doomed)))  # type: ignore[attr-defined]
    return PruneReport(applied=True,
                       reason=(f"pruned {len(doomed)} finished run(s) "
                               f"(run_max_runs={policy.run_max_runs}, "
                               f"run_max_age_ms={policy.run_max_age_ms})"),
                       policy=policy, removed_run_ids=tuple(sorted(doomed)),
                       removed_record_files=removed_files,
                       kept_runs=len(finalized) - len(doomed), skipped_active_run_id=active)


def prune_finished_runs_for_registry(registry: object, policy: RetentionPolicy, *,
                                     now_ms: Milliseconds) -> PruneReport:
    """向后兼容别名（显式命名意图）。"""
    return prune_finished_runs(registry, policy, now_ms=now_ms)


__all__ = ["PruneReport", "RetentionPolicy", "prune_finished_runs",
           "prune_finished_runs_for_registry"]
