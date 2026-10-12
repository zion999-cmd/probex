"""P0017.3 回测结果状态（六态，设计 §10）与原因码。

- 只有 SUCCESS 可被评估流程当作成功回测；失败态不可覆盖为成功。
- 离线、确定性、只读：本模块不触网、不碰真实交易路径。
"""

from __future__ import annotations

from enum import Enum


class BacktestStatus(str, Enum):
    SUCCESS = "SUCCESS"
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    REPLAY_FAILURE = "REPLAY_FAILURE"
    INCOMPLETE = "INCOMPLETE"

    @property
    def ok(self) -> bool:
        return self is BacktestStatus.SUCCESS


class BacktestError(Exception):
    """带回测状态分类的错误（driver 据此产出失败结果，而不是抛出裸异常）。"""

    def __init__(self, status: BacktestStatus, reason: str, *,
                 location: str | None = None) -> None:
        if status is BacktestStatus.SUCCESS:
            raise ValueError("BacktestError cannot use SUCCESS")
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.location = location


__all__ = ["BacktestStatus", "BacktestError"]
