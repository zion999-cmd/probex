"""产品侧 Market Readiness 契约（P0001.9.5 §3）。

把"public market 是否健康"从裸 `bool` 提升为**带来源与时间**的 typed evidence：

- 事实来自真实 public runtime（`FeatureEngine.book_health`、telemetry、历史 state）；
- 判定是**纯函数**（本模块不 import 任何 connector：connector 侧只负责读取事实并构造 evidence）；
- 每个 evidence 带 `generation`（market epoch）：invalidate / resync / 重连都会改变它，
  从而让过期的执行授权失效（§12 方案 A）。

与 `tests/live/market_readiness.py`（harness 侧）的关系：harness 的映射**委托**到本模块，避免两套逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import Milliseconds
from market.health.state import BookHealth


class MarketReadinessError(Exception):
    """Market readiness 契约错误（输入非法）。"""


@dataclass(frozen=True, slots=True)
class MarketReadinessPolicy:
    """判定阈值（**必须显式给出**；产品不提供默认业务值）。"""

    max_mark_age_ms: Milliseconds
    max_feed_age_ms: Milliseconds

    def __post_init__(self) -> None:
        for name in ("max_mark_age_ms", "max_feed_age_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MarketReadinessError(f"MarketReadinessPolicy.{name} must be a positive int")


@dataclass(frozen=True, slots=True)
class MarketReadinessEvidence:
    """一轮 public market 事实 + 判定结果（§3）。"""

    ready: bool
    observed_at: Milliseconds
    #: market epoch：invalidate / resync / 重连后必须变化（旧执行授权据此失效）
    generation: int
    book_health: str
    anchored: bool
    mark_age_ms: Milliseconds | None
    feed_age_ms: Milliseconds | None
    depth_gap_count: int
    resync_count: int
    malformed_count: int
    agg_trade_count: int
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ready, bool):
            raise MarketReadinessError("MarketReadinessEvidence.ready must be a bool")
        for name in ("observed_at", "generation", "depth_gap_count", "resync_count", "malformed_count",
                     "agg_trade_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MarketReadinessError(f"MarketReadinessEvidence.{name} must be a non-negative int")
        if not isinstance(self.anchored, bool):
            raise MarketReadinessError("MarketReadinessEvidence.anchored must be a bool")
        if not isinstance(self.book_health, str) or not self.book_health:
            raise MarketReadinessError("MarketReadinessEvidence.book_health must be a non-empty string")
        for name in ("mark_age_ms", "feed_age_ms"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise MarketReadinessError(f"MarketReadinessEvidence.{name} must be a non-negative int or None")
        if not isinstance(self.problems, tuple):
            raise MarketReadinessError("MarketReadinessEvidence.problems must be a tuple")


def market_ready(evidence: MarketReadinessEvidence, *, policy: MarketReadinessPolicy) -> tuple[bool, tuple[str, ...]]:
    """纯判定：把真实事实映射成 `ready` + 未满足原因（**不硬编码 true**，§3）。"""
    if not isinstance(evidence, MarketReadinessEvidence):
        raise MarketReadinessError("market_ready requires a MarketReadinessEvidence")
    if not isinstance(policy, MarketReadinessPolicy):
        raise MarketReadinessError("market_ready requires a MarketReadinessPolicy")
    problems: list[str] = []
    if evidence.book_health != BookHealth.HEALTHY.value:
        problems.append(f"book_health={evidence.book_health}")
    if not evidence.anchored:
        problems.append("snapshot anchor not established")
    if evidence.resync_count > 0:
        problems.append(f"resync_count={evidence.resync_count} in window")
    if evidence.depth_gap_count > 0:
        problems.append(f"depth_gap_count={evidence.depth_gap_count} in window")
    if evidence.malformed_count > 0:
        problems.append(f"malformed_count={evidence.malformed_count} in window")
    if evidence.agg_trade_count <= 0:
        problems.append("no aggTrade observed in window")
    if evidence.mark_age_ms is None:
        problems.append("mark price missing")
    elif evidence.mark_age_ms > policy.max_mark_age_ms:
        problems.append(f"mark_age_ms={evidence.mark_age_ms} > {policy.max_mark_age_ms}")
    if evidence.feed_age_ms is None:
        problems.append("no market state produced yet")
    elif evidence.feed_age_ms > policy.max_feed_age_ms:
        problems.append(f"feed_age_ms={evidence.feed_age_ms} > {policy.max_feed_age_ms}")
    return (not problems), tuple(problems)


def build_market_evidence(
    *,
    observed_at: Milliseconds,
    generation: int,
    book_health: str,
    anchored: bool,
    mark_age_ms: Milliseconds | None,
    feed_age_ms: Milliseconds | None,
    depth_gap_count: int,
    resync_count: int,
    malformed_count: int,
    agg_trade_count: int,
    policy: MarketReadinessPolicy,
) -> MarketReadinessEvidence:
    """由真实事实构造 evidence 并立即判定（`ready` 与 `problems` 总是一致）。"""
    provisional = MarketReadinessEvidence(
        ready=True,  # 占位，随判定覆盖
        observed_at=observed_at,
        generation=generation,
        book_health=book_health,
        anchored=anchored,
        mark_age_ms=mark_age_ms,
        feed_age_ms=feed_age_ms,
        depth_gap_count=depth_gap_count,
        resync_count=resync_count,
        malformed_count=malformed_count,
        agg_trade_count=agg_trade_count,
    )
    ready, problems = market_ready(provisional, policy=policy)
    return MarketReadinessEvidence(
        ready=ready,
        observed_at=observed_at,
        generation=generation,
        book_health=book_health,
        anchored=anchored,
        mark_age_ms=mark_age_ms,
        feed_age_ms=feed_age_ms,
        depth_gap_count=depth_gap_count,
        resync_count=resync_count,
        malformed_count=malformed_count,
        agg_trade_count=agg_trade_count,
        problems=problems,
    )


__all__ = [
    "MarketReadinessError",
    "MarketReadinessEvidence",
    "MarketReadinessPolicy",
    "build_market_evidence",
    "market_ready",
]
