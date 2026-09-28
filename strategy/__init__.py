"""策略层（P0001.7 起）。

策略层消费市场状态、预测、持仓与风险快照，产出 `OrderProposal` 级别的决策；
它不接触网络、不修改 Accounting、不驱动订单生命周期（那是 execution 层的职责）。
"""

from __future__ import annotations

__all__: list[str] = []
