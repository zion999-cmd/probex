"""Runtime 层（P0001.11.1 / P0001.11.2）：会话生命周期 + 生产接线。

本层只拥有**会话/运行身份、run 登记与生命周期监听**，不拥有交易决策
（strategy / risk / execution 仍各有其 Owner）。
"""

from runtime.session import RuntimeSession, RuntimeSessionError, SessionSummaryFacts
from runtime.wiring import (
    SessionHost,
    WiringError,
    open_live_session,
    open_paper_session,
    open_replay_session,
    open_session,
    open_testnet_session,
)

__all__ = [
    "RuntimeSession",
    "RuntimeSessionError",
    "SessionHost",
    "SessionSummaryFacts",
    "WiringError",
    "open_live_session",
    "open_paper_session",
    "open_replay_session",
    "open_session",
    "open_testnet_session",
]
