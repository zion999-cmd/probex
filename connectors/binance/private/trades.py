"""外部成交事实归一化（P0001.9.3 §1）：Binance `userTrades` → `ExternalFill`。

`/fapi/v1/userTrades` **只给 `orderId`**，没有 `clientOrderId` ⇒ 必须由调用方提供
`orderId → clientOrderId` 映射（来自 `openOrders ∪ allOrders`）。

P0001.9.3.1 SC-1 / SC-2 起，成交分**三类**（不再是「能/不能归属」两类）：

| 类别 | 判定 | 处理 |
| --- | --- | --- |
| 自己的 | 映射存在且 clientOrderId 严格匹配 `probex-` | 归一化为 `ExternalFill` → reconcile |
| 已验证外部 | 映射存在但 clientOrderId **不是**我们的 | 丢弃并计数（`foreign_order_ids`），不 adopt、不按 0 |
| 不可归属 | 映射**不存在**（orderId 从未出现在事实窗口内） | `unresolved_order_ids` ⇒ 调用方必须 fail closed（BLOCKED: UNRESOLVED_FILLS） |

"未知" 不得降级为 telemetry：不可归属的成交意味着「我们可能漏了真实成交」，必须阻塞恢复流程。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from execution.types import ExternalFill
from market.events.types import Venue

from connectors.binance.market_data.parsing import (
    require_decimal,
    require_field,
    require_int,
    require_mapping,
    require_optional_str,
    require_str,
)
from connectors.binance.private.errors import translate_market_errors, PrivateFormatError
from connectors.binance.private.orders import is_probex_order


@dataclass(frozen=True, slots=True)
class ExternalFillFacts:
    """归一化成交 + 三分类结果。"""

    fills: tuple[ExternalFill, ...]
    #: **已验证外部**订单（同账户内人工/其他机器人）的 orderId（去重、升序）；这些成交不属于我们
    foreign_order_ids: tuple[int, ...]
    #: 已验证外部的**成交条数**（同一个外部订单可能有多笔成交）
    foreign_fill_count: int
    #: **不可归属**的 orderId（去重、升序）：既非已知 Probex，也无法证明是外部 ⇒ fail closed
    unresolved_order_ids: tuple[int, ...]


@translate_market_errors
def parse_external_fills(
    raw: object,
    *,
    symbol: str,
    order_id_to_client_id: Mapping[int, str],
    venue: Venue = Venue.BINANCE,
    fee_asset: str = "USDT",
) -> ExternalFillFacts:
    """解析 `userTrades` 响应；只保留能**严格归属到 Probex 订单**的成交。"""
    if not isinstance(symbol, str) or not symbol:
        raise PrivateFormatError("symbol must be a non-empty string")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise PrivateFormatError("userTrades response must be an array")
    fills: list[ExternalFill] = []
    foreign: set[int] = set()
    foreign_fills = 0
    unresolved: set[int] = set()
    for entry in raw:
        item = require_mapping(entry, path="userTrades[]")
        trade_symbol = require_str(require_field(item, "symbol", path="userTrades[]"), path="userTrades[].symbol")
        if trade_symbol != symbol:
            raise PrivateFormatError(f"userTrades[].symbol: expected {symbol!r}, got {trade_symbol!r}")
        order_id = require_int(require_field(item, "orderId", path="userTrades[]"), path="userTrades[].orderId")
        client_order_id = order_id_to_client_id.get(order_id)
        if client_order_id is None:
            # 未知：orderId 从未出现在 openOrders/allOrders 事实窗口内 ⇒ 不可归属（fail closed）
            unresolved.add(order_id)
            continue
        if not is_probex_order(client_order_id):
            # 已验证外部：映射存在且明确不是我们的订单 ⇒ 丢弃并计数（D-037 §0.3 ownership boundary）
            foreign.add(order_id)
            foreign_fills += 1
            continue
        asset = require_optional_str(item.get("commissionAsset"), path="userTrades[].commissionAsset") or fee_asset
        if asset != fee_asset:
            raise PrivateFormatError(
                f"userTrades[].commissionAsset: {asset!r} != settlement asset {fee_asset!r} "
                "(不做多币种换算)"
            )
        trade_id = require_int(require_field(item, "id", path="userTrades[]"), path="userTrades[].id")
        fills.append(
            ExternalFill(
                client_order_id=client_order_id,
                execution_id=f"binance-trade-{trade_id}",
                trade_id=str(trade_id),
                price=require_decimal(require_field(item, "price", path="userTrades[]"), path="userTrades[].price"),
                quantity=require_decimal(require_field(item, "qty", path="userTrades[]"), path="userTrades[].qty"),
                timestamp=require_int(require_field(item, "time", path="userTrades[]"), path="userTrades[].time"),
                fee=require_decimal(require_field(item, "commission", path="userTrades[]"), path="userTrades[].commission"),
                fee_asset=asset,
            )
        )
    return ExternalFillFacts(
        fills=tuple(fills),
        foreign_order_ids=tuple(sorted(foreign)),
        foreign_fill_count=foreign_fills,
        unresolved_order_ids=tuple(sorted(unresolved)),
    )


__all__ = ["ExternalFillFacts", "parse_external_fills"]
