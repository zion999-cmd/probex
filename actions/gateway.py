"""Action Gateway（P0001.12.3 §4）：AI / UI / CLI 的**唯一受控操作入口**。

关键边界：

- 本模块**不 import** `execution` / `connectors` / `strategy` / `risk` / `live`；
- handler 只能由应用层注册，且**拒绝注册 CAPITAL handler**（结构上无法通过 Gateway 下单）；
- 需要确认的动作必须先拿到绑定 `action_id + parameters + runtime_id` 的确认；
- 每次调用（含拒绝）都写审计。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from market.events.types import Milliseconds

from actions.audit import ActionAuditEntry, ActionAuditLog, parameter_fingerprint
from actions.confirmation import ConfirmationError, ConfirmationRegistry
from actions.manifest import catalog_payload, spec
from actions.types import (
    ActionResult,
    ActionAvailability,
    ActionLevel,
    ActionRequest,
    ActionStatus,
)
from product.types import RuntimeMode


class ActionGatewayError(RuntimeError):
    """Gateway 使用错误（注册非法 handler 等）。"""


class ActionOutcomeUnknown(RuntimeError):
    """handler 明确表示"结果未知"（绝不当成 FAILED/SUCCEEDED）。"""


@dataclass(frozen=True, slots=True)
class HandlerResult:
    result: dict[str, object] = field(default_factory=dict)
    fact_refs: tuple[str, ...] = ()


ActionHandler = Callable[[ActionRequest, "ActionContext"], HandlerResult]


@dataclass(frozen=True, slots=True)
class ActionContext:
    """调用时的运行上下文（只引用 Product facts，不复制领域状态）。"""

    runtime_id: str
    mode: RuntimeMode
    environment: str
    venue: str
    symbol: str
    surface: str | None = None
    selected: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ActionGateway:
    """受控 Action Plane（与 `ProductService` 配对：一个读事实，一个执行动作）。"""

    clock: Callable[[], Milliseconds]
    confirmations: ConfirmationRegistry
    audit: ActionAuditLog = field(default_factory=ActionAuditLog)
    _handlers: dict[str, ActionHandler] = field(default_factory=dict)

    # ------------------------------------------------------------------ 注册

    def register(self, action_id: str, handler: ActionHandler) -> None:
        """注册一个 action handler（CAPITAL 一律拒绝）。"""
        item = spec(action_id)
        if item is None:
            raise ActionGatewayError(f"unknown action {action_id!r} (not in the manifest)")
        if item.level is ActionLevel.CAPITAL:
            raise ActionGatewayError(
                f"refusing to register a CAPITAL handler for {action_id!r}: "
                "capital actions stay unavailable_by_design"
            )
        if not callable(handler):
            raise ActionGatewayError("handler must be callable")
        self._handlers[action_id] = handler

    @property
    def registered(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def manifest(self) -> tuple[dict[str, object], ...]:
        """Manifest = 目录 + 运行时可用性（handler 是否注册）。"""
        payload = []
        for item in catalog_payload():
            entry = dict(item)
            if entry["availability"] == ActionAvailability.AVAILABLE.value:
                entry["available"] = entry["action_id"] in self._handlers
                if not entry["available"]:
                    entry["availability"] = ActionAvailability.UNAVAILABLE_NO_ENTRY_POINT.value
                    entry["unavailable_reason"] = "no handler is registered in this runtime"
            else:
                entry["available"] = False
            payload.append(entry)
        return tuple(payload)

    # ------------------------------------------------------------------ 调用

    def invoke(self, request: ActionRequest, context: ActionContext) -> ActionResult:
        started = int(self.clock())
        item = spec(request.action_id)
        if item is None:
            # 未知 action 也必须留痕（SC-9）：它没有 level，用 READ 作为审计档位并记录原因
            return self._finish(request, context, started, ActionStatus.REFUSED, reason="UNKNOWN_ACTION",
                                level=ActionLevel.READ)
        if item.level is ActionLevel.CAPITAL or item.availability is not ActionAvailability.AVAILABLE:
            return self._finish(request, context, started, ActionStatus.REFUSED,
                                reason="UNAVAILABLE_BY_DESIGN" if item.level is ActionLevel.CAPITAL
                                else item.availability.value, level=item.level)
        if context.mode not in item.allowed_modes:
            return self._finish(request, context, started, ActionStatus.REFUSED,
                                reason=f"MODE_NOT_ALLOWED:{context.mode.value}", level=item.level)
        if item.confirmation_required:
            if request.confirmation is None:
                confirmation = self.confirmations.issue(action_id=item.action_id,
                                                        parameters=dict(request.parameters),
                                                        runtime_id=context.runtime_id,
                                                        now_ms=started)
                return self._finish(request, context, started, ActionStatus.CONFIRMATION_REQUIRED,
                                    reason="CONFIRMATION_REQUIRED", level=item.level,
                                    confirmation_id=confirmation.confirmation_id)
            try:
                self.confirmations.consume(request.confirmation, action_id=item.action_id,
                                           parameters=dict(request.parameters),
                                           runtime_id=context.runtime_id, now_ms=started)
            except ConfirmationError as exc:
                return self._finish(request, context, started, ActionStatus.REFUSED,
                                    reason=f"CONFIRMATION_INVALID:{exc}", level=item.level,
                                    confirmation_id=request.confirmation)
        handler = self._handlers.get(item.action_id)
        if handler is None:
            return self._finish(request, context, started, ActionStatus.REFUSED, reason="NO_HANDLER",
                                level=item.level)
        try:
            outcome = handler(request, context)
        except ActionOutcomeUnknown as exc:
            return self._finish(request, context, started, ActionStatus.UNKNOWN, reason=str(exc),
                                level=item.level)
        except Exception as exc:  # noqa: BLE001 - 任何 handler 失败都必须如实记录
            return self._finish(request, context, started, ActionStatus.FAILED,
                                reason=f"{type(exc).__name__}", level=item.level)
        return self._finish(request, context, started, ActionStatus.SUCCEEDED, level=item.level,
                            result=dict(outcome.result), fact_refs=tuple(outcome.fact_refs),
                            confirmation_id=request.confirmation)

    # ------------------------------------------------------------------ 内部

    def _finish(self, request: ActionRequest, context: ActionContext, started: int,
                status: ActionStatus, *, level: ActionLevel | None, reason: str | None = None,
                result: dict[str, object] | None = None, fact_refs: tuple[str, ...] = (),
                confirmation_id: str | None = None) -> ActionResult:
        ended = int(self.clock())
        if level is not None:
            self.audit.record(ActionAuditEntry(
                ts=started, actor=request.requested_by, action_id=request.action_id, level=level,
                status=status, parameters_fingerprint=parameter_fingerprint(dict(request.parameters)),
                context_reference=request.context_reference, reason_code=reason,
                confirmation_id=confirmation_id,
            ))
        return ActionResult(action_id=request.action_id, status=status, started_at=started, ended_at=ended,
                            result=dict(result or {}), reason_code=reason, resulting_fact_refs=fact_refs,
                            confirmation_id=confirmation_id)


__all__ = ["ActionContext", "ActionGateway", "ActionGatewayError", "ActionHandler", "ActionOutcomeUnknown",
           "HandlerResult"]
