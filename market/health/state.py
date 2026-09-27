"""BookHealth：盘口可信度状态机。

四态由 P0001.1 §1.2 定义，不得增删：

```text
AWAITING_SNAPSHOT  --snapshot applied-->  RESYNCING
RESYNCING          --buffer replayed-->   HEALTHY
RESYNCING          --gap in replay-->     STALE
HEALTHY            --delta gap----------> STALE
HEALTHY            --snapshot applied-->  RESYNCING
STALE              --resync requested-->  RESYNCING
STALE              --snapshot applied-->  RESYNCING
```

只要状态不是 `HEALTHY`，可交易门即关闭（不输出交易可用状态）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class BookHealth(Enum):
    """盘口可信度。"""

    #: 本次会话尚未应用任何快照，盘口不可用。
    AWAITING_SNAPSHOT = "awaiting_snapshot"
    #: 盘口已同步且序号连续，可输出交易可用状态。
    HEALTHY = "healthy"
    #: 检测到 sequence gap，盘口不可用，等待重新同步。
    STALE = "stale"
    #: 正在重新同步：已请求新快照（等待中）或已应用快照（正在重放缓冲增量）。
    RESYNCING = "resyncing"


ALLOWED_TRANSITIONS: Mapping[BookHealth, frozenset[BookHealth]] = {
    BookHealth.AWAITING_SNAPSHOT: frozenset({BookHealth.RESYNCING}),
    BookHealth.RESYNCING: frozenset({BookHealth.HEALTHY, BookHealth.STALE}),
    BookHealth.HEALTHY: frozenset({BookHealth.STALE, BookHealth.RESYNCING}),
    BookHealth.STALE: frozenset({BookHealth.RESYNCING}),
}


class IllegalHealthTransition(RuntimeError):
    """非法状态转换。"""


def can_transition(source: BookHealth, target: BookHealth) -> bool:
    """`source -> target` 是否为合法转换。同态视为 no-op，不算合法转换。"""
    return target in ALLOWED_TRANSITIONS[source]


def require_transition(source: BookHealth, target: BookHealth) -> None:
    """校验转换合法性，非法则抛出 `IllegalHealthTransition`。"""
    if not can_transition(source, target):
        raise IllegalHealthTransition(f"illegal BookHealth transition: {source.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class HealthTransition:
    """一次已发生的状态转换记录，用于观测与测试。"""

    from_health: BookHealth
    to_health: BookHealth
    reason: str
