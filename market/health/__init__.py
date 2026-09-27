"""BookHealth 状态机模块。"""

from __future__ import annotations

from market.health.state import (
    ALLOWED_TRANSITIONS,
    BookHealth,
    HealthTransition,
    IllegalHealthTransition,
    can_transition,
    require_transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BookHealth",
    "HealthTransition",
    "IllegalHealthTransition",
    "can_transition",
    "require_transition",
]
