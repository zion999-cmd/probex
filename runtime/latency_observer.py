"""真实边界的执行延迟观测（closure Slice 3 / F-05）。

旁路：只把既有边界事实配成阶段样本，**不改变交易路径**，也不制造样本。

阶段与真实边界：

- `decision_to_submit`   ：orchestrator 决策（`decision.at_ms`）→ engine 提交请求
- `submit_to_ack`        ：engine 提交请求 → `OrderAccepted` 事件
- `cancel_to_ack`        ：engine 撤销请求 → `OrderStatusUpdate(CANCELED)` 更新
- `event_receive_lag`    ：私有流 corrected lag（`raw + offset`，D-036）
- `reconciliation_duration`：受控 reconciliation 入口耗时（已有的真实采样）

负值（时钟偏斜）不记录为样本，只累加计数（不 clamp、不伪造）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from execution_safety.latency import BoundedLatencyLog, LatencySample


@dataclass(slots=True)
class ExecutionLatencyObserver:
    log: BoundedLatencyLog
    clock: Callable[[], int]
    _submit_pending: int | None = None
    _cancel_pending: int | None = None
    _decision_pending: int | None = None
    skipped_negative: int = 0
    observed: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------ 边界回调

    def note(self, kind: str, ts_ms: int) -> None:
        """engine 边界回调（`submit_request` / `cancel_request` / `event:*` / `update:*`）。"""
        if kind == "submit_request":
            self._submit_pending = int(ts_ms)
            if self._decision_pending is not None:
                self._record("decision_to_submit", int(ts_ms) - self._decision_pending, now_ms=ts_ms)
                self._decision_pending = None
        elif kind == "cancel_request":
            self._cancel_pending = int(ts_ms)
        elif kind == "event:OrderAccepted" and self._submit_pending is not None:
            self._record("submit_to_ack", int(ts_ms) - self._submit_pending, now_ms=ts_ms)
            self._submit_pending = None
        elif kind == "update:Canceled" or kind == "update:OrderCanceled":
            if self._cancel_pending is not None:
                self._record("cancel_to_ack", int(ts_ms) - self._cancel_pending, now_ms=ts_ms)
                self._cancel_pending = None

    def note_decision(self, decision_at_ms: int) -> None:
        """orchestrator 决策边界（`decision.at_ms`）——等待 submit 请求到达。"""
        self._decision_pending = int(decision_at_ms)

    def note_private_event_lag(self, *, raw_lag_ms: int, offset_ms: int, now_ms: int | None = None) -> None:
        """私有流事件延迟（D-036：corrected = raw + offset）。"""
        self._record("event_receive_lag", int(raw_lag_ms) + int(offset_ms),
                     now_ms=int(self.clock() if now_ms is None else now_ms))

    def note_reconciliation_duration(self, started_ms: int, *, now_ms: int | None = None) -> None:
        self._record("reconciliation_duration", int(self.clock()) - int(started_ms),
                     now_ms=int(now_ms if now_ms is not None else self.clock()))

    # ------------------------------------------------------------------ 内部

    def _record(self, stage: str, value_ms: int, *, now_ms: int) -> None:
        if value_ms < 0:
            self.skipped_negative += 1
            return
        self.log.record(LatencySample(stage=stage, ts=int(now_ms), value_ms=int(value_ms)))
        self.observed[stage] = self.observed.get(stage, 0) + 1


__all__ = ["ExecutionLatencyObserver"]
