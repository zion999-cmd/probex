"""P0017.3 离线确定性预测运行时工厂。

回测复用现有 LocalTrialProvider（确定性、无随机性、无网络），
时钟锚定传入的历史时间，避免 wall-clock。
"""

from __future__ import annotations

from prediction.runtime import PredictionRuntime
from prediction.types import Clock, PredictionMode
from prediction.providers.local_trial import LocalTrialProvider


class FixedClock(Clock):
    """可显式设置 now() 的时钟（回测用，无 wall-clock）。"""

    def __init__(self) -> None:
        self._now = 0

    def set(self, ms: int) -> None:
        self._now = int(ms)

    def now(self) -> int:
        return self._now


def local_trial_runtime(*, timeout_ms: int = 1_000, ttl_ms: int = 120_000) -> tuple[PredictionRuntime, FixedClock]:
    clock = FixedClock()
    runtime = PredictionRuntime(
        provider=LocalTrialProvider(),
        clock=clock,
        timeout_ms=timeout_ms,
        ttl_ms=ttl_ms,
        mode=PredictionMode.LIVE_REQUERY)
    return runtime, clock


__all__ = ["FixedClock", "local_trial_runtime"]
