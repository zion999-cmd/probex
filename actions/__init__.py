"""Action Plane（P0001.12.3）：Manifest + Gateway + Confirmation + Audit。

`ActionGateway` 是 UI / CLI / Agent 的**唯一**受控操作入口；CAPITAL 恒为 `unavailable_by_design`。
"""

from actions.audit import ActionAuditEntry, ActionAuditLog, parameter_fingerprint
from actions.confirmation import Confirmation, ConfirmationError, ConfirmationRegistry
from actions.gateway import (
    ActionContext,
    ActionGateway,
    ActionGatewayError,
    ActionHandler,
    ActionOutcomeUnknown,
    HandlerResult,
)
from actions.manifest import ACTION_CATALOG, CATALOG_BY_ID, catalog_payload, spec
from actions.types import (
    ActionAvailability,
    ActionResult,
    ActionLevel,
    ActionRequest,
    ActionSpec,
    ActionStatus,
)

__all__ = [
    "ACTION_CATALOG",
    "ActionAuditEntry",
    "ActionAuditLog",
    "ActionAvailability",
    "ActionContext",
    "ActionGateway",
    "ActionGatewayError",
    "ActionHandler",
    "ActionLevel",
    "ActionOutcomeUnknown",
    "ActionResult",
    "ActionRequest",
    "ActionSpec",
    "ActionStatus",
    "CATALOG_BY_ID",
    "Confirmation",
    "ConfirmationError",
    "ConfirmationRegistry",
    "HandlerResult",
    "catalog_payload",
    "parameter_fingerprint",
    "spec",
]
