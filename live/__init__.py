"""Live 编排层（P0001.9.7）：把已各自验证的链路串成一个 **Testnet live loop**。

```text
MarketState → PredictionRuntime → MakerPolicy → fresh RiskSnapshot/RiskGate
            → ExecutionReadinessAuthority → BinanceExecutionAdapter
            → OrderTracker / Accounting
```

本层**只做 wiring + 生命周期 + 失败行为**：

- 不算价格 / 不算 size / 不算风险 / 不生成 prediction / 不解释 Jev（那些仍属既有层）；
- 不修改 `MakerPolicy`、`RiskGate`、`OrderTracker`、adapter 契约；
- **不接 Mainnet**：本阶段只做 Testnet 连续运行验证。
"""

from __future__ import annotations

from live.orchestrator import (
    ExecutionDisabledError,
    LiveExecutionOrchestrator,
    LoopOutcome,
    LoopPhase,
    OrchestratorConfig,
    OrchestratorError,
    OrchestratorState,
    OrderMultiplicityViolation,
    ReconciliationOutcome,
    StopReport,
)
from live.telemetry import LoopTelemetry, LoopTelemetrySink

__all__ = [
    "ExecutionDisabledError",
    "LiveExecutionOrchestrator",
    "LoopOutcome",
    "LoopPhase",
    "LoopTelemetry",
    "LoopTelemetrySink",
    "OrderMultiplicityViolation",
    "OrchestratorConfig",
    "OrchestratorError",
    "OrchestratorState",
    "ReconciliationOutcome",
    "StopReport",
]
