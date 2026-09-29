"""Run Summary 类型（P0001.10.2 §4 / §5）：报告全部来自**事实记录**。

- 报告必须绑定唯一 run identity（runtime_id / mode / environment / symbol / started_at /
  config fingerprints / data range），不能只输出一个孤立 PnL；
- 缺失事实一律 `Fact.unknown(...)`（不写 0）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    schema_version: str = SCHEMA_VERSION
