"""Unified Blockers（P0001.11 §4 / 裁决 D）：统一回答"为什么现在不动作？"。

纪律（来自裁决 D）：

- **不人为排序成单一最高优先级**：本模块不重排、不丢弃，只做投影与去重；
- 每条 blocker 至少带 `owner` / `reason_code` / `severity` / `message` / `source_ref`；
- severity 只有 `BLOCKING` / `DEGRADED` / `INFO`；
- 去重键 = `owner + reason_code + source_ref`（保持首次出现顺序）；
- 只投影**已知事实**：知道才产 blocker，不知道不编造。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from product.snapshot import SystemSnapshot
from product.types import BlockerOwner, BlockerSeverity, BlockerView, Fact


def dedupe_blockers(blockers: list[BlockerView] | tuple[BlockerView, ...]) -> tuple[BlockerView, ...]:
    """按去重键去重，**保持首次出现顺序**（不排序、不丢弃不同来源的同类原因）。"""
    seen: set[str] = set()
    result: list[BlockerView] = []
    for blocker in blockers:
        if blocker.key in seen:
            continue
        seen.add(blocker.key)
        result.append(blocker)
    return tuple(result)


#: orchestrator note 前缀 → (severity, reason_code)。未列出的 note ⇒ INFO（保留但降级，不丢弃）
_ORCHESTRATOR_NOTES: dict[str, tuple[BlockerSeverity, str]] = {
    "not_ready": (BlockerSeverity.BLOCKING, "ORCHESTRATOR_NOT_READY"),
    "reconciling": (BlockerSeverity.BLOCKING, "ORCHESTRATOR_RECONCILING"),
    "unknown_exposure": (BlockerSeverity.BLOCKING, "UNKNOWN_EXPOSURE"),
    "uncertain_exposure": (BlockerSeverity.BLOCKING, "UNCERTAIN_EXPOSURE"),
    "authority_invalid": (BlockerSeverity.BLOCKING, "AUTHORITY_INVALID"),
    "execution_disabled": (BlockerSeverity.BLOCKING, "EXECUTION_DISABLED"),
    "write_not_allowed": (BlockerSeverity.BLOCKING, "WRITE_NOT_ALLOWED"),
    "risk_reject": (BlockerSeverity.BLOCKING, "RISK_REJECT"),
}


def project_blockers(
    *,
    snapshot: SystemSnapshot | None = None,
    orchestrator_notes: tuple[str, ...] | list[str] = (),
) -> tuple[BlockerView, ...]:
    """把分散的阻塞原因投影成统一的 `BlockerView[]`（readiness / risk / strategy / orchestrator / market / prediction）。"""
    blockers: list[BlockerView] = []

    if snapshot is not None:
        readiness = snapshot.readiness
        status = readiness.status
        if not status.known:
            blockers.append(BlockerView(owner=BlockerOwner.READINESS, reason_code="READINESS_UNKNOWN",
                                        severity=BlockerSeverity.BLOCKING,
                                        message=status.reason or "readiness not evaluated",
                                        source_ref="readiness.status"))
        elif getattr(readiness, "applicable", None) is not None and readiness.applicable.known \
                and readiness.applicable.value is False:
            pass                                            # PAPER/REPLAY：记录但**不是** blocker
        else:
            for index, reason in enumerate(readiness.reasons):
                detail = readiness.details[index] if index < len(readiness.details) else reason
                blockers.append(BlockerView(owner=BlockerOwner.READINESS, reason_code=reason,
                                            severity=BlockerSeverity.BLOCKING, message=detail,
                                            source_ref=f"readiness.reasons[{index}]"))
        for index, reason in enumerate(snapshot.risk.rejects):
            blockers.append(BlockerView(owner=BlockerOwner.RISK, reason_code=reason,
                                        severity=BlockerSeverity.BLOCKING,
                                        message=f"risk gate rejected: {reason}",
                                        source_ref=f"risk.rejects[{index}]"))
        blocked_by = snapshot.strategy.blocked_by
        if blocked_by.known and blocked_by.value not in (None, ""):
            detail = snapshot.strategy.detail.value if snapshot.strategy.detail.known else None
            blockers.append(BlockerView(owner=BlockerOwner.STRATEGY, reason_code=str(blocked_by.value),
                                        severity=BlockerSeverity.BLOCKING,
                                        # decision 的 detail 可能为空 ⇒ 用稳定说明，不伪造原因
                                        message=(detail or "maker policy is not quoting"),
                                        source_ref="strategy.blocked_by"))
        market = snapshot.market
        if market.healthy.known and market.healthy.value is False:
            blockers.append(BlockerView(owner=BlockerOwner.MARKET, reason_code="MARKET_UNHEALTHY",
                                        severity=BlockerSeverity.BLOCKING,
                                        message="market data / book health gate is not satisfied",
                                        source_ref="market.healthy"))
        if market.tradeable.known and market.tradeable.value is False:
            blockers.append(BlockerView(owner=BlockerOwner.MARKET, reason_code="MARKET_NOT_TRADEABLE",
                                        severity=BlockerSeverity.BLOCKING,
                                        message="market state is not tradeable",
                                        source_ref="market.tradeable"))
        fresh = snapshot.prediction.freshest
        if fresh.known and fresh.value is False:
            blockers.append(BlockerView(owner=BlockerOwner.PREDICTION, reason_code="PREDICTION_STALE",
                                        severity=BlockerSeverity.BLOCKING,
                                        message="prediction is stale or unavailable for new increasing exposure",
                                        source_ref="prediction.freshest"))

    for index, note in enumerate(orchestrator_notes):
        head, _, remainder = str(note).partition(":")
        severity, reason_code = _ORCHESTRATOR_NOTES.get(head, (BlockerSeverity.INFO, head.upper() or "NOTE"))
        blockers.append(BlockerView(owner=BlockerOwner.ORCHESTRATOR, reason_code=reason_code,
                                    severity=severity, message=remainder or head,
                                    source_ref=f"orchestrator.notes[{index}]"))

    return dedupe_blockers(blockers)


def blocking_count(blockers: tuple[BlockerView, ...]) -> int:
    """便捷统计（只读，供 UI/报告；不参与任何判定）。"""
    return sum(1 for blocker in blockers if blocker.severity is BlockerSeverity.BLOCKING)


def blocker_owner_facts(*, owner: BlockerOwner) -> Fact:
    """占位工具：把 owner 变成一个可序列化事实（UI 排序用，不参与判定）。"""
    return Fact.of(owner.value)


__all__ = [
    "blocker_owner_facts",
    "blocking_count",
    "dedupe_blockers",
    "project_blockers",
]
