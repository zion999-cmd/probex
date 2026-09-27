"""不可信 JSON 报文的收敛工具（CLAUDE.md §17）。

所有外部字段在进入业务类型之前必须经由此处窄化：只接受明确的类型，
拒绝 `bool` 冒充数字、拒绝非有限数值，并把载荷层错误统一为 `MarketDataFormatError`。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from market.events.errors import InvalidPayloadError
from market.events.payloads import PriceLevel

from connectors.binance.market_data.errors import MarketDataFormatError


def require_mapping(value: object, *, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MarketDataFormatError(f"{path}: expected an object, got {type(value).__name__}")
    return value


def require_field(message: Mapping[str, object], key: str, *, path: str) -> object:
    try:
        return message[key]
    except KeyError:
        raise MarketDataFormatError(f"{path}: missing required field {key!r}") from None


def require_str(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise MarketDataFormatError(f"{path}: expected a non-empty string, got {type(value).__name__}")
    return value


def require_int(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketDataFormatError(f"{path}: expected an integer, got {type(value).__name__}")
    return value


def _require_sequence(value: object, *, path: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MarketDataFormatError(f"{path}: expected an array, got {type(value).__name__}")
    return value


def _require_decimal(value: object, *, path: str) -> float:
    """Binance 用字符串传输十进制数量。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise MarketDataFormatError(f"{path}: expected a decimal number, got {type(value).__name__}")
    try:
        number = float(value)
    except ValueError:
        raise MarketDataFormatError(f"{path}: {value!r} is not a decimal number") from None
    if not math.isfinite(number):
        raise MarketDataFormatError(f"{path}: expected a finite number, got {value!r}")
    return number


def require_levels(value: object, *, path: str) -> tuple[PriceLevel, ...]:
    """把 `[["price", "qty"], ...]` 形式的档位数组解析为 `PriceLevel` 元组。"""
    items = _require_sequence(value, path=path)
    levels: list[PriceLevel] = []
    for index, item in enumerate(items):
        entry_path = f"{path}[{index}]"
        entry = _require_sequence(item, path=entry_path)
        if len(entry) != 2:
            raise MarketDataFormatError(f"{entry_path}: expected [price, size], got {len(entry)} elements")
        price = _require_decimal(entry[0], path=f"{entry_path}[0]")
        size = _require_decimal(entry[1], path=f"{entry_path}[1]")
        try:
            levels.append(PriceLevel(price=price, size=size))
        except InvalidPayloadError as exc:
            raise MarketDataFormatError(f"{entry_path}: {exc}") from exc
    return tuple(levels)
