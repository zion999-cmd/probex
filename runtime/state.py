"""Runtime / loop state（closure Slice 1 / F-11）。

回答一个产品问题：**现在 runtime 是否真的在运行 / 是否在报价？**

- 状态机：STARTING → RUNNING → STOPPING → STOPPED，异常 ⇒ FAILED（不伪造 COMPLETED）；
- `quoting` 只在有真实依据时给出（无行情源 ⇒ `False` 且 detail 说明"idle: no event source"），
  绝不把"不知道"写成 `True`；
- Owner：`RuntimeStatusTracker` 是这些字段的唯一可写者；Product 层只读投影。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from market.events.types import Milliseconds

from product.types import RuntimeMode


class RuntimeState(Enum):
    """runtime 生命周期状态（产品表面据此回答"在不在跑"）。"""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: RuntimeState
    mode: RuntimeMode
    detail: str
    since_ms: Milliseconds
    started_at_ms: Milliseconds
    run_id: str | None = None
    quoting: bool | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in (RuntimeState.STOPPED, RuntimeState.FAILED)


@dataclass(slots=True)
class RuntimeStatusTracker:
    """runtime 状态的唯一 Owner（只在装配入口处被写入）。"""

    mode: RuntimeMode
    _state: RuntimeState = RuntimeState.STARTING
    _detail: str = "assembly started"
    _since_ms: Milliseconds = 0
    _started_at_ms: Milliseconds = 0
    _run_id: str | None = None
    _quoting: bool | None = None
    _error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode):
            raise TypeError("RuntimeStatusTracker.mode must be a RuntimeMode")

    # ------------------------------------------------------------------ mutation（仅 Owner）

    def mark_starting(self, *, now_ms: Milliseconds, detail: str = "starting") -> RuntimeStatus:
        return self._set(RuntimeState.STARTING, now_ms=now_ms, detail=detail)

    def mark_running(self, *, now_ms: Milliseconds, run_id: str, quoting: bool | None,
                     detail: str) -> RuntimeStatus:
        self._run_id = run_id
        return self._set(RuntimeState.RUNNING, now_ms=now_ms, detail=detail, quoting=quoting)

    def mark_quoting(self, *, now_ms: Milliseconds, quoting: bool | None, detail: str) -> RuntimeStatus:
        return self._set(RuntimeState.RUNNING, now_ms=now_ms, detail=detail, quoting=quoting)

    def mark_stopping(self, *, now_ms: Milliseconds, detail: str = "graceful stop requested") -> RuntimeStatus:
        return self._set(RuntimeState.STOPPING, now_ms=now_ms, detail=detail)

    def mark_stopped(self, *, now_ms: Milliseconds, detail: str = "stopped") -> RuntimeStatus:
        return self._set(RuntimeState.STOPPED, now_ms=now_ms, detail=detail, quoting=False)

    def mark_failed(self, *, now_ms: Milliseconds, error: str) -> RuntimeStatus:
        self._error = error
        return self._set(RuntimeState.FAILED, now_ms=now_ms, detail=f"failed: {error}", quoting=False)

    def _set(self, state: RuntimeState, *, now_ms: Milliseconds, detail: str,
             quoting: bool | None = None) -> RuntimeStatus:
        self._state = state
        self._detail = detail
        self._since_ms = int(now_ms)
        if self._started_at_ms == 0:
            self._started_at_ms = int(now_ms)
        if quoting is not None or state in (RuntimeState.STOPPED, RuntimeState.FAILED):
            self._quoting = quoting
        return self.status()

    # ------------------------------------------------------------------ read（投影用）

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(state=self._state, mode=self.mode, detail=self._detail,
                             since_ms=self._since_ms, started_at_ms=self._started_at_ms,
                             run_id=self._run_id, quoting=self._quoting, error=self._error)


__all__ = ["RuntimeState", "RuntimeStatus", "RuntimeStatusTracker"]
