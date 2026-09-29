"""Runtime 层（P0001.11.1）：把 Run Registry 接到真实 runtime lifecycle。

本层只拥有**会话/运行身份与 run 登记**，不拥有交易决策（strategy / risk / execution 仍各有其 Owner）。
"""

from runtime.session import RuntimeSession, RuntimeSessionError, SessionSummaryFacts

__all__ = ["RuntimeSession", "RuntimeSessionError", "SessionSummaryFacts"]
