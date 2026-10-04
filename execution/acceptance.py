"""TESTNET acceptance capability（P0001.16 §14 B + 人类裁决 2026-10-04「选择 B」）。

**性质：这是 TESTNET acceptance capability，不是新的产品交易能力。**

用途**仅限**验收真实供应商执行工程闭环：

```
submit → ACK → private user stream → TRADE/FILLED → FillLedger → AccountingCore → Product/UI → 最终 flat
```

人类裁决的严格限制（本模块把它们做成**结构性**约束，而不是文档约定）：

1. 只允许 **marketable LIMIT / IOC**（IOC 永不挂单 ⇒ 不产生残留挂单）；
2. **默认不存在**：只有 composition root 在**显式 flag** 下构造 `TestnetAcceptancePermission` 并注入 Binance
   adapter；产品默认路径（`MakerPolicy` 永远是 `post_only=True`）**没有** taker 能力；
3. **TESTNET only / BTCUSDT only / notional ≤ 100 USDT**（构造期 + 每次授权两次校验）；
4. 仍必须经过 `Risk → Readiness → ExecutionEngine → PrivateExecutionConnector → Binance`：
   本模块只回答"adapter 是否可以接受一个非 post-only 订单"，**不**提供任何写入口、不绕过任何 Owner；
5. PAPER/LIVE 不受影响；Product UI / Assistant / Strategy / CAPITAL Action 都无法触发
   （它们要么不 import 本模块，要么不持有 permission 对象）。
"""

from __future__ import annotations

from dataclasses import dataclass

from execution.types import ExecutionError, Order

#: 显式 flag（默认关闭）；只有 composition root 读它。
ACCEPTANCE_FLAG_ENV = "PROBEX_TESTNET_ACCEPTANCE"
#: 人类裁决的 notional 上限（USDT）。
DEFAULT_MAX_NOTIONAL = 100.0
#: 本阶段只允许这一个 symbol（D-034 边界）。
ACCEPTANCE_SYMBOL = "BTCUSDT"
#: 唯一的 time-in-force：IOC（要么立即成交、要么立即取消 —— 绝不挂单）。
ACCEPTANCE_TIME_IN_FORCE = "IOC"


class AcceptanceError(ExecutionError):
    """acceptance capability 的契约错误。"""


#: 唯一允许的环境标签（本模块不依赖 readiness 类型；由 composition root 传入其 `.value`）
TESTNET_ENVIRONMENT_LABEL = "testnet"


@dataclass(frozen=True, slots=True)
class TestnetAcceptancePermission:
    """一次显式授予的 TESTNET acceptance 许可（不可变、可审计、逐单校验）。"""

    symbol: str
    max_notional: float
    granted_by: str
    environment: str = TESTNET_ENVIRONMENT_LABEL

    def __post_init__(self) -> None:
        if not isinstance(self.environment, str) or self.environment.lower() != TESTNET_ENVIRONMENT_LABEL:
            raise AcceptanceError(
                f"acceptance permission is TESTNET only, got {self.environment!r}")
        if self.symbol != ACCEPTANCE_SYMBOL:
            raise AcceptanceError(f"acceptance permission is {ACCEPTANCE_SYMBOL} only, got {self.symbol!r}")
        if not isinstance(self.max_notional, (int, float)) or isinstance(self.max_notional, bool):
            raise AcceptanceError("acceptance permission requires a numeric max_notional")
        if self.max_notional <= 0.0 or self.max_notional > DEFAULT_MAX_NOTIONAL:
            raise AcceptanceError(
                f"acceptance permission max_notional must be in (0, {DEFAULT_MAX_NOTIONAL}], "
                f"got {self.max_notional!r}")
        if not isinstance(self.granted_by, str) or not self.granted_by:
            raise AcceptanceError("acceptance permission must record who granted it (audit)")

    def authorize(self, *, order: Order) -> tuple[bool, str]:
        """逐单校验（adapter 在每次非 post-only submit 前调用）。"""
        if not isinstance(order, Order):
            raise AcceptanceError("authorize() requires a local Order")
        if order.post_only:
            return True, "post_only_order_needs_no_acceptance_permission"
        if order.symbol != self.symbol:
            return False, f"acceptance_symbol_mismatch:{order.symbol}"
        notional = float(order.price) * float(order.quantity)
        if notional > self.max_notional:
            return False, f"acceptance_notional_exceeds_cap:{notional}>{self.max_notional}"
        return True, f"acceptance_granted_by:{self.granted_by}"

    def view(self) -> dict[str, object]:
        return {"symbol": self.symbol, "max_notional": self.max_notional,
                "granted_by": self.granted_by, "environment": self.environment,
                "time_in_force": ACCEPTANCE_TIME_IN_FORCE, "post_only": False}


def testnet_acceptance_permission(
    *,
    environment: str,
    symbol: str,
    enabled: bool = False,
    max_notional: float = DEFAULT_MAX_NOTIONAL,
    granted_by: str = ACCEPTANCE_FLAG_ENV,
) -> TestnetAcceptancePermission | None:
    """构造许可；**任何一项不满足都返回 `None`**（能力不存在，而不是"部分可用"）。

    `enabled` 必须由 composition root 从显式 flag 传入（默认 `False`）。
    本模块不读环境变量、不依赖 readiness 类型（execution 层的依赖白名单）。
    """
    if not isinstance(environment, str):
        raise AcceptanceError("testnet_acceptance_permission requires an environment label string")
    if not enabled:
        return None
    if environment.lower() != TESTNET_ENVIRONMENT_LABEL:
        return None
    if symbol != ACCEPTANCE_SYMBOL:
        return None
    if not isinstance(max_notional, (int, float)) or isinstance(max_notional, bool):
        raise AcceptanceError("max_notional must be numeric")
    if max_notional <= 0.0:
        return None
    return TestnetAcceptancePermission(symbol=symbol, max_notional=float(min(max_notional, DEFAULT_MAX_NOTIONAL)),
                                       granted_by=granted_by, environment=environment)


__all__ = [
    "ACCEPTANCE_FLAG_ENV", "ACCEPTANCE_SYMBOL", "ACCEPTANCE_TIME_IN_FORCE", "DEFAULT_MAX_NOTIONAL",
    "TESTNET_ENVIRONMENT_LABEL", "AcceptanceError", "TestnetAcceptancePermission",
    "testnet_acceptance_permission",
]
