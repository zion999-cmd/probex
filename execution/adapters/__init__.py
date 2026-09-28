"""Execution adapters：外部事实来源。"""

from __future__ import annotations

from execution.adapters.base import ExecutionAdapter
from execution.adapters.paper import PaperBroker, PaperRejectReason

__all__ = ["ExecutionAdapter", "PaperBroker", "PaperRejectReason"]
