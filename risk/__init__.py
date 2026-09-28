"""风险领域：限额、快照与强制闸门。

Risk 只消费不可变 `RiskSnapshot`，不直接读取 mutable accounting 对象，也不 import
Prediction / Jev / Strategy / Execution。
"""

from __future__ import annotations

from risk.gate import RiskGate, classify_exposure
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import (
    ExposureClass,
    InvalidOrderProposalError,
    OrderProposal,
    RiskDecision,
    RiskDecisionType,
    RiskError,
    RiskReasonCode,
    RiskSnapshot,
)

__all__ = [
    "ExposureClass",
    "InvalidOrderProposalError",
    "OrderProposal",
    "RiskDecision",
    "RiskDecisionType",
    "RiskError",
    "RiskGate",
    "RiskLimits",
    "RiskReasonCode",
    "RiskSnapshot",
    "build_risk_snapshot",
    "classify_exposure",
    "utc_day_start_ms",
]
