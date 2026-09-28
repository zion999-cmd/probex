"""持仓事实（P0001.9.2 §0.4）：`GET /fapi/v2/positionRisk` → `PositionObservation`。

第一版只接受 **one-way 模式 + USDT-M + 单一 symbol**；检测到 hedge mode（`positionSide != "BOTH"`）
或非 USDT 保证金资产 ⇒ `UnsupportedAccountModeError`（fail closed，不做自动兼容）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from market.events.types import Milliseconds

from connectors.binance.market_data.parsing import (
    require_decimal,
    require_field,
    require_int,
    require_mapping,
    require_optional_int,
    require_optional_str,
    require_str,
)
from connectors.binance.private.errors import translate_market_errors, PrivateFormatError, UnsupportedAccountModeError

#: one-way 模式下 Binance 给出的 `positionSide`。
ONE_WAY_POSITION_SIDE = "BOTH"
#: 本阶段唯一支持的结算/保证金资产（USDT-M）。
SUPPORTED_MARGIN_ASSET = "USDT"


class AccountMode(Enum):
    """账户持仓模式。"""

    ONE_WAY = "one_way"
    HEDGE = "hedge"


@dataclass(frozen=True, slots=True)
class PositionObservation:
    """单一 symbol 的持仓事实（含强平与杠杆信息）。"""

    symbol: str
    position_amt: float
    entry_price: float
    mark_price: float
    unrealized_profit: float
    liquidation_price: float
    leverage: int
    margin_type: str
    position_side: str
    margin_asset: str
    update_time_ms: Milliseconds | None
    receive_ts: Milliseconds
    process_ts: Milliseconds

    @property
    def account_mode(self) -> AccountMode:
        return AccountMode.ONE_WAY if self.position_side == ONE_WAY_POSITION_SIDE else AccountMode.HEDGE

    @property
    def is_flat(self) -> bool:
        return self.position_amt == 0.0

    @property
    def is_long(self) -> bool:
        return self.position_amt > 0.0

    @property
    def is_short(self) -> bool:
        return self.position_amt < 0.0

    @property
    def notional(self) -> float:
        return abs(self.position_amt) * self.mark_price


def require_one_way_mode(*, position_side: str, margin_asset: str, symbol: str) -> AccountMode:
    """校验 one-way + USDT-M；不满足即 fail closed（不自动兼容）。"""
    if position_side != ONE_WAY_POSITION_SIDE:
        raise UnsupportedAccountModeError(
            f"{symbol}: positionSide={position_side!r} indicates hedge mode; "
            "P0001.9.2 only supports one-way mode (fail closed, no auto-adaptation)"
        )
    if margin_asset != SUPPORTED_MARGIN_ASSET:
        raise UnsupportedAccountModeError(
            f"{symbol}: margin asset {margin_asset!r} is not USDT-M; unsupported in this phase"
        )
    return AccountMode.ONE_WAY


@translate_market_errors
def parse_position_risk(
    raw: object,
    *,
    symbol: str,
    receive_ts: Milliseconds,
    process_ts: Milliseconds,
) -> PositionObservation:
    """从 `GET /fapi/v2/positionRisk` 响应中提取指定 symbol 的持仓事实。"""
    if not isinstance(symbol, str) or not symbol:
        raise PrivateFormatError("symbol must be a non-empty string")
    if not isinstance(raw, (list, tuple)):
        raise PrivateFormatError("positionRisk response must be an array")
    for entry in raw:
        item = require_mapping(entry, path="positionRisk[]")
        if item.get("symbol") == symbol:
            return parse_position_entry(item, receive_ts=receive_ts, process_ts=process_ts)
    raise PrivateFormatError(f"positionRisk response has no entry for symbol {symbol!r}")


@translate_market_errors
def parse_position_entry(
    entry: Mapping[str, object], *, receive_ts: Milliseconds, process_ts: Milliseconds
) -> PositionObservation:
    """解析单条 positionRisk 记录（account 内嵌 positions[] 亦可复用，字段可选时填 0/None）。"""
    path = "positionRisk"
    symbol = require_str(require_field(entry, "symbol", path=path), path=f"{path}.symbol")
    position_amt = require_decimal(require_field(entry, "positionAmt", path=path), path=f"{path}.positionAmt")
    entry_price = require_decimal(require_field(entry, "entryPrice", path=path), path=f"{path}.entryPrice")
    mark_price = require_decimal(require_field(entry, "markPrice", path=path), path=f"{path}.markPrice")
    unrealized = require_decimal(require_field(entry, "unRealizedProfit", path=path), path=f"{path}.unRealizedProfit")
    liquidation = require_decimal(require_field(entry, "liquidationPrice", path=path), path=f"{path}.liquidationPrice")
    leverage = require_int(require_field(entry, "leverage", path=path), path=f"{path}.leverage")
    margin_type = require_str(require_field(entry, "marginType", path=path), path=f"{path}.marginType")
    position_side = require_str(require_field(entry, "positionSide", path=path), path=f"{path}.positionSide")
    margin_asset = require_optional_str(entry.get("marginAsset"), path=f"{path}.marginAsset") or require_optional_str(
        entry.get("asset"), path=f"{path}.asset"
    )
    if margin_asset is None:
        raise PrivateFormatError(f"{path}: missing marginAsset/asset (cannot confirm USDT-M)")

    for name, value in (("entryPrice", entry_price), ("markPrice", mark_price), ("liquidationPrice", liquidation)):
        if not math.isfinite(value) or value < 0.0:
            raise PrivateFormatError(f"{path}.{name} must be a non-negative finite number, got {value!r}")
    if mark_price <= 0.0:
        raise PrivateFormatError(f"{path}.markPrice must be > 0, got {mark_price!r}")
    if leverage < 1:
        raise PrivateFormatError(f"{path}.leverage must be >= 1, got {leverage!r}")
    require_one_way_mode(position_side=position_side, margin_asset=margin_asset, symbol=symbol)

    return PositionObservation(
        symbol=symbol,
        position_amt=position_amt,
        entry_price=entry_price,
        mark_price=mark_price,
        unrealized_profit=unrealized,
        liquidation_price=liquidation,
        leverage=leverage,
        margin_type=margin_type,
        position_side=position_side,
        margin_asset=margin_asset,
        update_time_ms=require_optional_int(entry.get("updateTime"), path=f"{path}.updateTime"),
        receive_ts=receive_ts,
        process_ts=process_ts,
    )


__all__ = [
    "ONE_WAY_POSITION_SIDE",
    "SUPPORTED_MARGIN_ASSET",
    "AccountMode",
    "PositionObservation",
    "parse_position_entry",
    "parse_position_risk",
    "require_one_way_mode",
]
