"""Execution Latency（P0001.13 §4）：五阶段稳定定义 + 有界采样 + policy 驱动的 budget。

样本不足 ⇒ `UNKNOWN`（**不是 0 ms**，SC-7）。本模块不产生任何交易动作。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from market.events.types import Milliseconds

from execution_safety.policy import ExecutionSafetyPolicy
from execution_safety.types import LATENCY_STAGES, LatencyState, LatencyStats
from product.types import Fact


class LatencyStageError(ValueError):
    """未知阶段或非法样本。"""


@dataclass(frozen=True, slots=True)
class LatencySample:
    stage: str
    ts: Milliseconds
    value_ms: int

    def __post_init__(self) -> None:
        if self.stage not in LATENCY_STAGES:
            raise LatencyStageError(f"unknown latency stage {self.stage!r}")
        if isinstance(self.value_ms, bool) or not isinstance(self.value_ms, int) or self.value_ms < 0:
            raise LatencyStageError("LatencySample.value_ms must be a non-negative int")


@dataclass(slots=True)
class BoundedLatencyLog:
    """有界采样日志（运行时内存；不是新的交易事实）。"""

    capacity: int = 2_000
    _samples: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity <= 0:
            raise ValueError("BoundedLatencyLog.capacity must be a positive int")
        self._samples = deque(self._samples, maxlen=self.capacity)

    def record(self, sample: LatencySample) -> None:
        self._samples.append(sample)

    def samples(self, *, stage: str | None = None) -> tuple[LatencySample, ...]:
        return tuple(item for item in self._samples if stage is None or item.stage == stage)

    @property
    def counts(self) -> dict[str, int]:
        return {"samples": len(self._samples), "capacity": self.capacity}


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        raise ValueError("percentile requires samples")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(quantile * (len(ordered) - 1)))))
    return ordered[index]


def evaluate_latency(*, policy: ExecutionSafetyPolicy, log: BoundedLatencyLog,
                     now_ms: Milliseconds) -> LatencyState:
    """按 policy 的窗口 / 最小样本 / budget 计算五阶段统计（缺样本 ⇒ UNKNOWN）。"""
    if not isinstance(policy, ExecutionSafetyPolicy):
        raise ValueError("evaluate_latency requires an ExecutionSafetyPolicy")
    floor = int(now_ms) - int(policy.latency_window_ms)
    stats: list[LatencyStats] = []
    for stage in LATENCY_STAGES:
        values = [sample.value_ms for sample in log.samples(stage=stage) if sample.ts >= floor]
        budget = Fact.of(int(policy.latency_budget_ms[stage]))
        if len(values) < int(policy.latency_min_samples):
            unknown = Fact.unknown(
                f"insufficient samples ({len(values)} < {policy.latency_min_samples}) — not 0 ms"
            )
            stats.append(LatencyStats(stage=stage, status="UNKNOWN", p50=unknown, p95=unknown,
                                      maximum=unknown, sample_count=len(values), budget_ms=budget))
            continue
        p95 = _percentile(values, 0.95)
        status = "OK" if p95 <= int(policy.latency_budget_ms[stage]) else "BUDGET_EXCEEDED"
        stats.append(LatencyStats(stage=stage, status=status, p50=Fact.of(_percentile(values, 0.5)),
                                  p95=Fact.of(p95), maximum=Fact.of(max(values)),
                                  sample_count=len(values), budget_ms=budget))
    return LatencyState(stages=tuple(stats), window_ms=int(policy.latency_window_ms),
                        min_samples=int(policy.latency_min_samples))


__all__ = ["BoundedLatencyLog", "LatencySample", "LatencyStageError", "evaluate_latency"]
