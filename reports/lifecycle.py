"""Run lifecycle helper（P0001.11 §2 / 裁决 B）：runtime identity 创建时建 run，正常 stop 时 close。

这是给**运行时/runner**用的极薄封装（不是第二套状态机）：

- 进入时 `registry.start(...)`（绑定 runtime identity + config provenance）；
- 正常退出时 `registry.finalize(status=COMPLETED)`（追加 finalization record，不覆盖历史）；
- 异常/崩溃退出时 `registry.finalize(status=INCOMPLETE)` 并**重新抛出**原始异常；
- 不做 retention、不做并发协调（单 writer，裁决 B）。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType

from market.events.types import Milliseconds

from product.provenance import ConfigSnapshot
from product.types import Fact, RuntimeIdentity
from reports.types import RunRecord, RunStatus
from storage.run_registry import JsonRunRegistry


@dataclass(slots=True)
class RunLifecycle:
    """一次运行的登记/关闭（context manager）。"""

    registry: JsonRunRegistry
    runtime: RuntimeIdentity
    run_id: str
    clock: object
    config: ConfigSnapshot | None = None
    data_range: Fact | None = None
    _record: RunRecord | None = None

    def __enter__(self) -> RunRecord:
        self._record = self.registry.start(runtime=self.runtime, run_id=self.run_id,
                                           now_ms=int(self.clock()), config=self.config,
                                           data_range=self.data_range)
        return self._record

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> bool:
        assert self._record is not None, "RunLifecycle must be entered before it is exited"  # noqa: S101
        status = RunStatus.COMPLETED if exc_type is None else RunStatus.INCOMPLETE
        self.registry.finalize(run_id=self.run_id, ended_at=int(self.clock()),
                               summary=None if exc_type is not None else self._summary(),
                               status=status)
        return False  # 不吞异常（裁决 B：崩溃必须如实留下 INCOMPLETE）

    def _summary(self) -> dict[str, object] | None:
        return None


__all__ = ["RunLifecycle"]
