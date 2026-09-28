"""账户事实（P0001.9.2 §0.4）：`GET /fapi/v2/account` → `AccountSnapshotObservation`。

只做归一化与 fail-closed 校验：

- balance / availableBalance / walletBalance / marginBalance / unrealizedProfit 全部保留（字符串 → float，
  缺失或非法即报错）；结算资产以外的资产也保留，但不做换算（与 Accounting 的纪律一致）；
- 持仓模式：account 内的 `positions[].positionSide` 只要出现非 `BOTH` ⇒ `UnsupportedAccountModeError`；
- `dualSidePosition=True`（若响应包含该字段）⇒ 同样 fail closed。

完整持仓事实（positionAmt / markPrice / liquidationPrice / leverage …）来自 `positionRisk`，见 `positions.py`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from market.events.types import Milliseconds

from connectors.binance.market_data.parsing import (
    require_bool,
    require_decimal,
    require_field,
    require_int,
    require_mapping,
    require_optional_int,
    require_optional_str,
    require_str,
)
from connectors.binance.private.errors import translate_market_errors, PrivateFormatError, UnsupportedAccountModeError
from connectors.binance.private.positions import ONE_WAY_POSITION_SIDE, SUPPORTED_MARGIN_ASSET


@dataclass(frozen=True, slots=True)
class BalanceObservation:
    """单一资产的余额事实。"""

    asset: str
    balance: float
    available_balance: float
    wallet_balance: float
    margin_balance: float
    unrealized_profit: float
    max_withdraw_amount: float | None
    update_time_ms: Milliseconds | None

    @property
    def is_settlement_asset(self) -> bool:
        return self.asset == SUPPORTED_MARGIN_ASSET


@dataclass(frozen=True, slots=True)
class AccountSnapshotObservation:
    """一次账户快照事实（balances + 汇总 + 模式校验结论）。"""

    symbol: str
    can_trade: bool
    total_wallet_balance: float
    total_margin_balance: float
    total_unrealized_profit: float
    available_balance: float
    max_withdraw_amount: float | None
    update_time_ms: Milliseconds | None
    dual_side_position: bool | None
    balances: tuple[BalanceObservation, ...]
    position_sides: tuple[tuple[str, str], ...]
    receive_ts: Milliseconds
    process_ts: Milliseconds

    def balance(self, asset: str) -> BalanceObservation | None:
        for entry in self.balances:
            if entry.asset == asset:
                return entry
        return None

    @property
    def settlement_balance(self) -> BalanceObservation | None:
        """结算资产（USDT）余额；不存在时返回 None（未知 ≠ 0）。"""
        return self.balance(SUPPORTED_MARGIN_ASSET)


@translate_market_errors
def parse_account_snapshot(
    raw: object, *, symbol: str, receive_ts: Milliseconds, process_ts: Milliseconds
) -> AccountSnapshotObservation:
    """解析 `GET /fapi/v2/account` 响应。"""
    path = "account"
    if not isinstance(symbol, str) or not symbol:
        raise PrivateFormatError("symbol must be a non-empty string")
    message = require_mapping(raw, path=path)
    can_trade = require_bool(require_field(message, "canTrade", path=path), path=f"{path}.canTrade")
    total_wallet = require_decimal(require_field(message, "totalWalletBalance", path=path), path=f"{path}.totalWalletBalance")
    total_margin = require_decimal(require_field(message, "totalMarginBalance", path=path), path=f"{path}.totalMarginBalance")
    total_unrealized = require_decimal(
        require_field(message, "totalUnrealizedProfit", path=path), path=f"{path}.totalUnrealizedProfit"
    )
    available = require_decimal(require_field(message, "availableBalance", path=path), path=f"{path}.availableBalance")
    for name, value in (
        ("totalWalletBalance", total_wallet),
        ("totalMarginBalance", total_margin),
        ("availableBalance", available),
    ):
        if not math.isfinite(value):
            raise PrivateFormatError(f"{path}.{name} must be finite, got {value!r}")

    balances = _parse_balances(require_field(message, "assets", path=path), symbol=symbol)
    position_sides = _parse_position_sides(require_field(message, "positions", path=path))
    dual_side = message.get("dualSidePosition")
    dual_side_position = require_bool(dual_side, path=f"{path}.dualSidePosition") if dual_side is not None else None
    _require_supported_mode(symbol=symbol, position_sides=position_sides, dual_side_position=dual_side_position)

    return AccountSnapshotObservation(
        symbol=symbol,
        can_trade=can_trade,
        total_wallet_balance=total_wallet,
        total_margin_balance=total_margin,
        total_unrealized_profit=total_unrealized,
        available_balance=available,
        max_withdraw_amount=_optional_decimal(message.get("maxWithdrawAmount"), path=f"{path}.maxWithdrawAmount"),
        update_time_ms=require_optional_int(message.get("updateTime"), path=f"{path}.updateTime"),
        dual_side_position=dual_side_position,
        balances=balances,
        position_sides=position_sides,
        receive_ts=receive_ts,
        process_ts=process_ts,
    )


def _parse_balances(raw_assets: object, *, symbol: str) -> tuple[BalanceObservation, ...]:
    if isinstance(raw_assets, (str, bytes)) or not isinstance(raw_assets, Sequence):
        raise PrivateFormatError("account.assets must be an array")
    balances: list[BalanceObservation] = []
    for entry in raw_assets:
        item = require_mapping(entry, path="account.assets[]")
        asset = require_str(require_field(item, "asset", path="account.assets[]"), path="account.assets[].asset")
        balances.append(
            BalanceObservation(
                asset=asset,
                balance=require_decimal(require_field(item, "walletBalance", path=f"assets[{asset}]"), path="walletBalance"),
                available_balance=require_decimal(
                    require_field(item, "availableBalance", path=f"assets[{asset}]"), path="availableBalance"
                ),
                wallet_balance=require_decimal(
                    require_field(item, "walletBalance", path=f"assets[{asset}]"), path="walletBalance"
                ),
                margin_balance=require_decimal(
                    require_field(item, "marginBalance", path=f"assets[{asset}]"), path="marginBalance"
                ),
                unrealized_profit=require_decimal(
                    require_field(item, "unrealizedProfit", path=f"assets[{asset}]"), path="unrealizedProfit"
                ),
                max_withdraw_amount=_optional_decimal(item.get("maxWithdrawAmount"), path="maxWithdrawAmount"),
                update_time_ms=require_optional_int(item.get("updateTime"), path="assets[].updateTime"),
            )
        )
    if not balances:
        raise PrivateFormatError(f"account.assets is empty for symbol {symbol!r}")
    return tuple(balances)


def _parse_position_sides(raw_positions: object) -> tuple[tuple[str, str], ...]:
    if isinstance(raw_positions, (str, bytes)) or not isinstance(raw_positions, Sequence):
        raise PrivateFormatError("account.positions must be an array")
    sides: list[tuple[str, str]] = []
    for entry in raw_positions:
        item = require_mapping(entry, path="account.positions[]")
        symbol = require_str(require_field(item, "symbol", path="account.positions[]"), path="positions[].symbol")
        side = require_optional_str(item.get("positionSide"), path="positions[].positionSide") or ONE_WAY_POSITION_SIDE
        sides.append((symbol, side))
    return tuple(sides)


def _require_supported_mode(
    *, symbol: str, position_sides: tuple[tuple[str, str], ...], dual_side_position: bool | None
) -> None:
    if dual_side_position is True:
        raise UnsupportedAccountModeError(
            f"{symbol}: account reports dualSidePosition=True (hedge mode); P0001.9.2 supports one-way only"
        )
    for position_symbol, side in position_sides:
        if side != ONE_WAY_POSITION_SIDE:
            raise UnsupportedAccountModeError(
                f"{position_symbol}: positionSide={side!r} indicates hedge mode; P0001.9.2 supports one-way only"
            )


def _optional_decimal(value: object, *, path: str) -> float | None:
    if value is None:
        return None
    return require_decimal(value, path=path)


__all__ = ["AccountSnapshotObservation", "BalanceObservation", "parse_account_snapshot"]
