"""Action Audit Trail（P0001.12.3 §6）：产品操作审计，**不是**第二套交易 Event Store。

- 记录 who / what / when / context / parameters / result / reason；
- 有界内存缓冲（不落盘、不成为订单事实 Owner）；
- 参数以**指纹**形式记录（避免把可能敏感的输入原样留存）。
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field

from market.events.types import Milliseconds

from actions.types import ActionLevel, ActionStatus
from product.types import Fact


def parameter_fingerprint(parameters: dict[str, object]) -> str:
    text = json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ActionAuditEntry:
    ts: Milliseconds
    actor: str
    action_id: str
    level: ActionLevel
    status: ActionStatus
    parameters_fingerprint: str
    context_reference: str | None = None
    reason_code: str | None = None
    confirmation_id: str | None = None


@dataclass(slots=True)
class ActionAuditLog:
    """有界审计日志（运行时内存；不引入新的持久化系统）。"""

    capacity: int = 500
    _entries: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity <= 0:
            raise ValueError("ActionAuditLog.capacity must be a positive int")
        self._entries = deque(self._entries, maxlen=self.capacity)

    def record(self, entry: ActionAuditEntry) -> None:
        self._entries.append(entry)

    def entries(self) -> tuple[ActionAuditEntry, ...]:
        return tuple(self._entries)

    @property
    def counts(self) -> dict[str, int]:
        return {"entries": len(self._entries), "capacity": self.capacity}


__all__ = ["ActionAuditEntry", "ActionAuditLog", "parameter_fingerprint"]
