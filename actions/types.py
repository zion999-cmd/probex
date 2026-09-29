"""Action Plane 类型（P0001.12.3 §2/§3/§4）。

四级能力：L0 READ / L1 PRODUCT / L2 RUNTIME / L3 CAPITAL。
**CAPITAL 第一版全部 `unavailable_by_design`** —— 这是产品边界，不是临时状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from market.events.types import Milliseconds

from product.types import RuntimeMode


class ActionLevel(Enum):
    READ = "L0_READ"
    PRODUCT = "L1_PRODUCT"
    RUNTIME = "L2_RUNTIME"
    CAPITAL = "L3_CAPITAL"


class ActionStatus(Enum):
    """结果状态：`UNKNOWN` **不得**伪装成 `FAILED` 或 `SUCCEEDED`。"""

    SUCCEEDED = "SUCCEEDED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"


class ActionAvailability(Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE_BY_DESIGN = "UNAVAILABLE_BY_DESIGN"
    UNAVAILABLE_NO_ENTRY_POINT = "UNAVAILABLE_NO_ENTRY_POINT"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """一个 action 的正式契约（Manifest 的唯一来源；AI 不得自创 API）。"""

    action_id: str
    name: str
    level: ActionLevel
    parameters: tuple[str, ...] = ()
    side_effect: str = "none"
    confirmation_required: bool = False
    allowed_modes: tuple[RuntimeMode, ...] = tuple(RuntimeMode)
    result_schema: str = "{}"
    availability: ActionAvailability = ActionAvailability.AVAILABLE
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("ActionSpec.action_id must be a non-empty string")
        if not isinstance(self.level, ActionLevel):
            raise TypeError("ActionSpec.level must be an ActionLevel")
        if self.level is ActionLevel.CAPITAL and self.availability is ActionAvailability.AVAILABLE:
            raise ValueError(
                "CAPITAL actions must stay unavailable_by_design in this stage "
                "(no second trading control path)"
            )
        if self.availability is not ActionAvailability.AVAILABLE and not self.unavailable_reason:
            raise ValueError("unavailable action must carry an unavailable_reason")


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """一次 action 调用（来自 UI / CLI / Agent，三者共用同一 Gateway）。"""

    action_id: str
    parameters: dict[str, object] = field(default_factory=dict)
    requested_by: str = "unknown"
    confirmation: str | None = None
    context_reference: str | None = None


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    status: ActionStatus
    started_at: Milliseconds
    ended_at: Milliseconds
    result: dict[str, object] = field(default_factory=dict)
    reason_code: str | None = None
    resulting_fact_refs: tuple[str, ...] = ()
    confirmation_id: str | None = None


__all__ = [
    "ActionAvailability",
    "ActionResult",
    "ActionLevel",
    "ActionRequest",
    "ActionSpec",
    "ActionStatus",
]
