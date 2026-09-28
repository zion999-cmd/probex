"""User Data Stream 事件（P0001.9.2 §0.5）：事实归一化 + 去重/乱序检测。

本模块**只产出 Observation**：不写 OrderTracker、不写 Accounting、不驱动 ExecutionEngine。

处理的事件类型：`ACCOUNT_UPDATE`、`ORDER_TRADE_UPDATE`、`listenKeyExpired`；
其余类型（`MARGIN_CALL`、`ACCOUNT_CONFIG_UPDATE`…）标记为 `UNSUPPORTED` 并计数，不崩、不误当业务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from market.events.types import Milliseconds

from connectors.binance.market_data.parsing import (
    require_bool,
    require_decimal,
    require_field,
    require_int,
    require_mapping,
    require_optional_str,
    require_str,
)
from connectors.binance.private.errors import translate_market_errors, PrivateFormatError

#: 事件名常量。
ACCOUNT_UPDATE_EVENT = "ACCOUNT_UPDATE"
ORDER_TRADE_UPDATE_EVENT = "ORDER_TRADE_UPDATE"
LISTEN_KEY_EXPIRED_EVENT = "listenKeyExpired"


class UserEventType(Enum):
    """本阶段处理的事件类型。"""

    ACCOUNT_UPDATE = "acct_update"
    ORDER_TRADE_UPDATE = "order_trade_update"
    LISTEN_KEY_EXPIRED = "listen_key_expired"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class BalanceDeltaObservation:
    """`ACCOUNT_UPDATE` 中的资产变化事实。"""

    asset: str
    wallet_balance: float


@dataclass(frozen=True, slots=True)
class PositionDeltaObservation:
    """`ACCOUNT_UPDATE` 中的持仓变化事实。"""

    symbol: str
    position_amt: float
    entry_price: float
    unrealized_profit: float
    margin_type: str | None
    isolated_wallet: float | None


@dataclass(frozen=True, slots=True)
class AccountUpdateObservation:
    """`ACCOUNT_UPDATE` 事实。"""

    event_ts: Milliseconds
    transaction_ts: Milliseconds
    receive_ts: Milliseconds
    process_ts: Milliseconds
    reason: str
    balances: tuple[BalanceDeltaObservation, ...]
    positions: tuple[PositionDeltaObservation, ...]


@dataclass(frozen=True, slots=True)
class OrderUpdateObservation:
    """`ORDER_TRADE_UPDATE` 事实（含成交明细；**不修改任何本地订单状态**）。"""

    event_ts: Milliseconds
    transaction_ts: Milliseconds
    receive_ts: Milliseconds
    process_ts: Milliseconds
    symbol: str
    client_order_id: str
    order_id: int
    side: str
    order_type: str
    execution_type: str
    order_status: str
    last_fill_quantity: float
    cumulative_fill_quantity: float
    last_fill_price: float
    average_price: float
    commission: float
    commission_asset: str | None
    trade_id: int
    is_maker: bool
    original_quantity: float
    original_price: float
    reduce_only: bool

    @property
    def is_fill(self) -> bool:
        """是否为成交类事件（`last_fill_quantity > 0`）。"""
        return self.last_fill_quantity > 0.0


@dataclass(frozen=True, slots=True)
class ListenKeyExpiredObservation:
    """`listenKeyExpired` 事实：listenKey 已失效，必须重建。"""

    event_ts: Milliseconds
    receive_ts: Milliseconds
    process_ts: Milliseconds


UserStreamObservation = AccountUpdateObservation | OrderUpdateObservation | ListenKeyExpiredObservation


@dataclass(frozen=True, slots=True)
class UserEvent:
    """一条已解析的 user data stream 事件。"""

    event_type: UserEventType
    raw_type: str
    observation: UserStreamObservation | None = None

    @property
    def supported(self) -> bool:
        return self.event_type is not UserEventType.UNSUPPORTED


@translate_market_errors
def parse_user_event(
    raw: object, *, receive_ts: Milliseconds, process_ts: Milliseconds
) -> UserEvent:
    """解析一条 user data stream 报文（严格；未知事件类型不报错）。"""
    message = require_mapping(raw, path="userStreamEvent")
    raw_type = require_str(require_field(message, "e", path="userStreamEvent"), path="userStreamEvent.e")
    if raw_type == ACCOUNT_UPDATE_EVENT:
        return UserEvent(UserEventType.ACCOUNT_UPDATE, raw_type, _parse_account_update(message, receive_ts, process_ts))
    if raw_type == ORDER_TRADE_UPDATE_EVENT:
        return UserEvent(
            UserEventType.ORDER_TRADE_UPDATE, raw_type, _parse_order_update(message, receive_ts, process_ts)
        )
    if raw_type == LISTEN_KEY_EXPIRED_EVENT:
        event_ts = require_int(require_field(message, "E", path="userStreamEvent"), path="userStreamEvent.E")
        return UserEvent(
            UserEventType.LISTEN_KEY_EXPIRED,
            raw_type,
            ListenKeyExpiredObservation(
                event_ts=event_ts, receive_ts=receive_ts, process_ts=process_ts
            ),
        )
    return UserEvent(UserEventType.UNSUPPORTED, raw_type, None)


def _parse_account_update(
    message: Mapping[str, object], receive_ts: Milliseconds, process_ts: Milliseconds
) -> AccountUpdateObservation:
    path = ACCOUNT_UPDATE_EVENT
    event_ts = require_int(require_field(message, "E", path=path), path=f"{path}.E")
    transaction_ts = require_int(require_field(message, "T", path=path), path=f"{path}.T")
    update = require_mapping(require_field(message, "a", path=path), path=f"{path}.a")
    reason = require_str(require_field(update, "m", path=f"{path}.a"), path=f"{path}.a.m")
    balances = _parse_balance_deltas(require_field(update, "B", path=f"{path}.a"))
    positions = _parse_position_deltas(require_field(update, "P", path=f"{path}.a"))
    return AccountUpdateObservation(
        event_ts=event_ts,
        transaction_ts=transaction_ts,
        receive_ts=receive_ts,
        process_ts=process_ts,
        reason=reason,
        balances=balances,
        positions=positions,
    )


def _parse_balance_deltas(raw_balances: object) -> tuple[BalanceDeltaObservation, ...]:
    if isinstance(raw_balances, (str, bytes)) or not isinstance(raw_balances, Sequence):
        raise PrivateFormatError("ACCOUNT_UPDATE.a.B must be an array")
    deltas: list[BalanceDeltaObservation] = []
    for entry in raw_balances:
        item = require_mapping(entry, path="ACCOUNT_UPDATE.a.B[]")
        deltas.append(
            BalanceDeltaObservation(
                asset=require_str(require_field(item, "a", path="B[]"), path="B[].a"),
                wallet_balance=require_decimal(require_field(item, "wb", path="B[]"), path="B[].wb"),
            )
        )
    return tuple(deltas)


def _parse_position_deltas(raw_positions: object) -> tuple[PositionDeltaObservation, ...]:
    if isinstance(raw_positions, (str, bytes)) or not isinstance(raw_positions, Sequence):
        raise PrivateFormatError("ACCOUNT_UPDATE.a.P must be an array")
    deltas: list[PositionDeltaObservation] = []
    for entry in raw_positions:
        item = require_mapping(entry, path="ACCOUNT_UPDATE.a.P[]")
        deltas.append(
            PositionDeltaObservation(
                symbol=require_str(require_field(item, "s", path="P[]"), path="P[].s"),
                position_amt=require_decimal(require_field(item, "pa", path="P[]"), path="P[].pa"),
                entry_price=require_decimal(require_field(item, "ep", path="P[]"), path="P[].ep"),
                unrealized_profit=require_decimal(require_field(item, "up", path="P[]"), path="P[].up"),
                margin_type=require_optional_str(item.get("mt"), path="P[].mt"),
                isolated_wallet=(
                    require_decimal(item["iw"], path="P[].iw") if item.get("iw") is not None else None
                ),
            )
        )
    return tuple(deltas)


def _parse_order_update(
    message: Mapping[str, object], receive_ts: Milliseconds, process_ts: Milliseconds
) -> OrderUpdateObservation:
    path = ORDER_TRADE_UPDATE_EVENT
    order = require_mapping(require_field(message, "o", path=path), path=f"{path}.o")
    return OrderUpdateObservation(
        event_ts=require_int(require_field(message, "E", path=path), path=f"{path}.E"),
        transaction_ts=require_int(require_field(message, "T", path=path), path=f"{path}.T"),
        receive_ts=receive_ts,
        process_ts=process_ts,
        # 真实 Binance USDⓈ-M `ORDER_TRADE_UPDATE` 把 symbol 放在 `o.s`（**没有** top-level `s`）
        symbol=require_str(require_field(order, "s", path=f"{path}.o"), path=f"{path}.o.s"),
        client_order_id=require_str(require_field(order, "c", path=f"{path}.o"), path=f"{path}.o.c"),
        order_id=require_int(require_field(order, "i", path=f"{path}.o"), path=f"{path}.o.i"),
        side=require_str(require_field(order, "S", path=f"{path}.o"), path=f"{path}.o.S"),
        order_type=require_str(require_field(order, "o", path=f"{path}.o"), path=f"{path}.o.o"),
        execution_type=require_str(require_field(order, "x", path=f"{path}.o"), path=f"{path}.o.x"),
        order_status=require_str(require_field(order, "X", path=f"{path}.o"), path=f"{path}.o.X"),
        last_fill_quantity=require_decimal(require_field(order, "l", path=f"{path}.o"), path=f"{path}.o.l"),
        cumulative_fill_quantity=require_decimal(require_field(order, "z", path=f"{path}.o"), path=f"{path}.o.z"),
        last_fill_price=require_decimal(require_field(order, "L", path=f"{path}.o"), path=f"{path}.o.L"),
        average_price=require_decimal(require_field(order, "ap", path=f"{path}.o"), path=f"{path}.o.ap"),
        commission=require_decimal(require_field(order, "n", path=f"{path}.o"), path=f"{path}.o.n"),
        commission_asset=require_optional_str(order.get("N"), path=f"{path}.o.N"),
        trade_id=require_int(require_field(order, "t", path=f"{path}.o"), path=f"{path}.o.t"),
        is_maker=require_bool(require_field(order, "m", path=f"{path}.o"), path=f"{path}.o.m"),
        original_quantity=require_decimal(require_field(order, "q", path=f"{path}.o"), path=f"{path}.o.q"),
        original_price=require_decimal(require_field(order, "p", path=f"{path}.o"), path=f"{path}.o.p"),
        reduce_only=require_bool(require_field(order, "R", path=f"{path}.o"), path=f"{path}.o.R"),
    )


@dataclass
class UserEventOrdering:
    """去重 / 乱序检测（SC-11）。

    没有全局序号可用，因此按事实本身的单调量做水位：

    - `ORDER_TRADE_UPDATE`：每单 `cumulative_fill_quantity` 单调不减；回退 = 乱序，重复 = 重复。
    - `ACCOUNT_UPDATE`：`(transaction_ts, event_ts)` 水位。
    """

    duplicate_count: int = 0
    out_of_order_count: int = 0
    _order_watermark: dict[str, float] = field(default_factory=dict)
    _order_event_key: dict[str, tuple[float, int, str, str, int]] = field(default_factory=dict)
    _account_watermark: tuple[int, int] | None = None

    def accept_order_update(
        self,
        *,
        client_order_id: str,
        cumulative_fill_quantity: float,
        trade_id: int,
        execution_type: str,
        order_status: str,
        event_ts: int,
    ) -> bool:
        """判定订单事件是否应被消费。

        - **重复**：与上一条**完全相同**的事件（累计量 + trade id + execution type + 状态 + 事件时间）。
          注意不能只用（累计量, trade id）：真实 Binance 的 `NEW` → `CANCELED` 等**非成交**更新里
          两者都是 0 ⇒ 只看它们会把合法的状态变化误判成重复（测试网实测）。
        - **乱序**：累计成交量**回退**（单调量变小）⇒ 旧事件。
        """
        if not isinstance(client_order_id, str) or not client_order_id:
            raise PrivateFormatError("client_order_id must be a non-empty string")
        key = (cumulative_fill_quantity, trade_id, execution_type, order_status, event_ts)
        previous_qty = self._order_watermark.get(client_order_id)
        if previous_qty is not None:
            if cumulative_fill_quantity < previous_qty:
                self.out_of_order_count += 1
                return False
            if self._order_event_key.get(client_order_id) == key:
                self.duplicate_count += 1
                return False
        self._order_watermark[client_order_id] = cumulative_fill_quantity
        self._order_event_key[client_order_id] = key
        return True

    def accept_account_update(self, *, transaction_ts: int, event_ts: int) -> bool:
        """水位比较：更旧 ⇒ 乱序；完全相同 ⇒ 重复。"""
        current = (transaction_ts, event_ts)
        if self._account_watermark is not None:
            if current == self._account_watermark:
                self.duplicate_count += 1
                return False
            if current < self._account_watermark:
                self.out_of_order_count += 1
                return False
        self._account_watermark = current
        return True

    def accept(self, event: UserEvent) -> bool:
        """按事件类型分派去重/乱序判定；不支持的与 expired 一律接受。"""
        if event.event_type is UserEventType.ORDER_TRADE_UPDATE and isinstance(
            event.observation, OrderUpdateObservation
        ):
            observation = event.observation
            return self.accept_order_update(
                client_order_id=observation.client_order_id,
                cumulative_fill_quantity=observation.cumulative_fill_quantity,
                trade_id=observation.trade_id,
                execution_type=observation.execution_type,
                order_status=observation.order_status,
                event_ts=observation.event_ts,
            )
        if event.event_type is UserEventType.ACCOUNT_UPDATE and isinstance(event.observation, AccountUpdateObservation):
            return self.accept_account_update(
                transaction_ts=event.observation.transaction_ts,
                event_ts=event.observation.event_ts,
            )
        return True

    @property
    def tracked_orders(self) -> int:
        return len(self._order_watermark)



__all__ = [
    "ACCOUNT_UPDATE_EVENT",
    "LISTEN_KEY_EXPIRED_EVENT",
    "ORDER_TRADE_UPDATE_EVENT",
    "AccountUpdateObservation",
    "BalanceDeltaObservation",
    "ListenKeyExpiredObservation",
    "OrderUpdateObservation",
    "PositionDeltaObservation",
    "UserEvent",
    "UserEventOrdering",
    "UserEventType",
    "UserStreamObservation",
    "parse_user_event",
]
