"""Confirmation 契约（P0001.12.3 §5）：绑定 action + 参数 + runtime_id + 过期时间。

规则：

- 旧确认不能用于新动作（参数被指纹绑定）；
- 不能跨 runtime 重放；
- 一次性（consume 后失效）；
- 过期即失效（时钟由调用方注入，产品层不读 wall-clock）。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from market.events.types import Milliseconds

from actions.audit import parameter_fingerprint


class ConfirmationError(RuntimeError):
    """确认无效（过期 / 不匹配 / 已使用）。"""


@dataclass(frozen=True, slots=True)
class Confirmation:
    confirmation_id: str
    action_id: str
    parameters_fingerprint: str
    runtime_id: str
    issued_at_ms: Milliseconds
    expires_at_ms: Milliseconds


@dataclass(slots=True)
class ConfirmationRegistry:
    """待确认动作登记（一次性、TTL 由调用方显式给出）。"""

    ttl_ms: Milliseconds
    _pending: dict[str, Confirmation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.ttl_ms, bool) or not isinstance(self.ttl_ms, int) or self.ttl_ms <= 0:
            raise ValueError("ConfirmationRegistry.ttl_ms must be a positive int")

    def issue(self, *, action_id: str, parameters: dict[str, object], runtime_id: str,
              now_ms: Milliseconds) -> Confirmation:
        confirmation = Confirmation(
            confirmation_id=f"confirm-{secrets.token_hex(8)}",
            action_id=action_id,
            parameters_fingerprint=parameter_fingerprint(parameters),
            runtime_id=runtime_id,
            issued_at_ms=int(now_ms),
            expires_at_ms=int(now_ms) + int(self.ttl_ms),
        )
        self._pending[confirmation.confirmation_id] = confirmation
        return confirmation

    def consume(self, confirmation_id: str, *, action_id: str, parameters: dict[str, object],
                runtime_id: str, now_ms: Milliseconds) -> Confirmation:
        confirmation = self._pending.pop(confirmation_id, None)
        if confirmation is None:
            raise ConfirmationError("unknown or already used confirmation")
        if int(now_ms) > confirmation.expires_at_ms:
            raise ConfirmationError("confirmation expired")
        if confirmation.action_id != action_id:
            raise ConfirmationError("confirmation does not match this action")
        if confirmation.runtime_id != runtime_id:
            raise ConfirmationError("confirmation does not match this runtime")
        if confirmation.parameters_fingerprint != parameter_fingerprint(parameters):
            raise ConfirmationError("confirmation does not match these parameters")
        return confirmation

    @property
    def pending(self) -> int:
        return len(self._pending)


__all__ = ["Confirmation", "ConfirmationError", "ConfirmationRegistry"]
