"""Run Summary 类型（P0001.10.2 §4 / §5）：报告全部来自**事实记录**。

- 报告必须绑定唯一 run identity（runtime_id / mode / environment / symbol / started_at /
  config fingerprints / data range），不能只输出一个孤立 PnL；
- 缺失事实一律 `Fact.unknown(...)`（不写 0）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from market.events.types import Milliseconds

from product.types import SCHEMA_VERSION, Fact, RuntimeIdentity


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """一次运行的唯一身份（§5：报告必须绑定 run identity）。"""

    run_id: str
    runtime: RuntimeIdentity
    started_at: Milliseconds
    ended_at: Fact
    config_fingerprints: dict[str, Fact] = field(default_factory=dict)
    data_range: Fact = field(default_factory=lambda: Fact.unknown("data range not provided"))
    #: 绑定的 config provenance id（P0001.11 SC-2：每个 run 都绑定 config fingerprint / id）
    config_id: Fact = field(default_factory=lambda: Fact.unknown("no config snapshot bound"))

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("RunIdentity.run_id must be a non-empty string")
        if not isinstance(self.runtime, RuntimeIdentity):
            raise TypeError("RunIdentity.runtime must be a RuntimeIdentity")
        if not isinstance(self.ended_at, Fact) or not isinstance(self.data_range, Fact):
            raise TypeError("RunIdentity.ended_at / data_range must be Facts")
        for key, value in self.config_fingerprints.items():
            if not isinstance(key, str) or not isinstance(value, Fact):
                raise TypeError("config_fingerprints must map str -> Fact")


@dataclass(frozen=True, slots=True)
class RunSummary:
    """一次运行的结构化总结（REPLAY / PAPER / TESTNET 共用同一形状）。"""

    run: RunIdentity
    duration_ms: Milliseconds
    market_health: dict[str, object]
    prediction: dict[str, object]
    decision_counts: dict[str, int]
    order_counts: dict[str, int]
    fills: Fact
    fees: Fact
    realized_pnl: Fact
    unrealized_pnl: Fact
    max_exposure: Fact
    final_position: Fact
    readiness_blockers: tuple[str, ...] = ()
    anomalies: tuple[str, ...] = ()
    #: Metric Contract 结果的产品形态（`Fact` payload；报告不重算，UI 不重算）
    metrics: dict[str, object] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


class RunStatus(Enum):
    """运行状态（裁决 B：崩溃未 close ⇒ `INCOMPLETE`，不是 UNKNOWN）。"""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Run Registry 的记录（P0001.11 §2）：**只存运行索引与结果引用**，不是第二套 Accounting。"""

    run_id: str
    runtime: RuntimeIdentity
    started_at: Milliseconds
    ended_at: Fact
    status: RunStatus
    config_id: Fact
    config_fingerprint: Fact
    data_range: Fact
    #: 运行结果引用（`RunSummary` 的 fact payload）；未 finalize ⇒ None
    summary: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("RunRecord.run_id must be a non-empty string")
        if not isinstance(self.runtime, RuntimeIdentity):
            raise TypeError("RunRecord.runtime must be a RuntimeIdentity")
        if not isinstance(self.status, RunStatus):
            raise TypeError("RunRecord.status must be a RunStatus")
        for name in ("ended_at", "config_id", "config_fingerprint", "data_range"):
            if not isinstance(getattr(self, name), Fact):
                raise TypeError(f"RunRecord.{name} must be a Fact")
        # 注：`COMPLETED` 允许没有 summary —— 那表示"结果未记录"（compare/metrics 一律 UNKNOWN），
        # 而不是用空对象冒充结果（未知 ≠ 空）。

    @property
    def is_final(self) -> bool:
        """是否已有 finalization（`INCOMPLETE` = 崩溃遗留，仍可被显式 finalize）。"""
        return self.status is RunStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """两个 run 的同名指标比较（裁决 F：一边 UNKNOWN ⇒ delta = UNKNOWN）。"""

    name: str
    left: Fact
    right: Fact
    delta: Fact


@dataclass(frozen=True, slots=True)
class RunComparison:
    """两个 run 的对照（按同名 metric 对齐；只有两边 known 才给 delta）。"""

    left_run_id: str
    right_run_id: str
    metrics: tuple[MetricComparison, ...] = ()

    def metric(self, name: str) -> MetricComparison | None:
        return next((item for item in self.metrics if item.name == name), None)
