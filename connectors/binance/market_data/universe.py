"""Universe 发现（P0017.1 §2.1）：exchangeInfo → 可订阅交易对集合。

与 P0001.9.1 的单 symbol 规则解析**并列**，不改变 `parse_exchange_info(raw, symbol=...)` 行为。

纪律
----
- 不硬编码交易对：全部来自真实 `GET /fapi/v1/exchangeInfo` 的 `symbols[]`。
- 只读筛选：永续 + `status=TRADING` + `quoteAsset ∈ 允许集合`（默认 USDT）。
- 本阶段**不**用价格/成交量做流动性门槛（无权威可靠字段前不猜）。
- 畸形条目跳过并计入 `skipped`（fail loud on structure error，fail soft on per-item filter），
  绝不让一个坏条目污染整个 universe，也不伪造交易对。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.parsing import (
    require_field,
    require_mapping,
    require_str,
)

#: Binance USDⓈ-M 永续的 contractType 形态（含带日期的变体）。
_PERPETUAL_CONTRACT_TYPES = frozenset({"PERPETUAL"})
#: 人类 2026-10-11 确认：首期只筛 USDT 本位永续。
DEFAULT_QUOTE_ASSETS: frozenset[str] = frozenset({"USDT"})
TRADING_STATUS = "TRADING"


@dataclass(frozen=True, slots=True)
class UniverseSymbol:
    """一个被发现的可订阅标的（不可变；只含发现事实，不含实时行情）。"""

    symbol: str
    base_asset: str
    quote_asset: str
    settlement_asset: str
    contract_type: str
    status: str

    def view(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "base_asset": self.base_asset,
            "asset_class": "CRYPTO",
            "product_type": "PERPETUAL",
            "quote_asset": self.quote_asset,
            "settlement_asset": self.settlement_asset,
            "contract_type": self.contract_type,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class Universe:
    """发现结果：DISCOVERED（可订阅）+ 被跳过条目（含原因，供 UI/审计展示）。"""

    discovered: tuple[UniverseSymbol, ...]
    skipped: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    quote_assets: tuple[str, ...] = ("USDT",)

    def get(self, symbol: str) -> UniverseSymbol | None:
        for item in self.discovered:
            if item.symbol == symbol:
                return item
        return None

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.discovered)

    def view(self) -> dict[str, object]:
        return {
            "quote_assets": list(self.quote_assets),
            "discovered_count": len(self.discovered),
            "skipped_count": len(self.skipped),
            "symbols": [item.view() for item in self.discovered],
            "skipped": [{"symbol": s, "reason": r} for s, r in self.skipped],
        }


def parse_universe(
    raw: object,
    *,
    quote_assets: frozenset[str] | tuple[str, ...] | set[str] = DEFAULT_QUOTE_ASSETS,
    contract_types: frozenset[str] = _PERPETUAL_CONTRACT_TYPES,
) -> Universe:
    """从 exchangeInfo 响应解析可订阅 universe。

    - 顶层结构非法 ⇒ MarketDataFormatError（fail closed）。
    - 单个条目缺字段/不匹配筛选 ⇒ 计入 skipped（不抛错，不污染其余标的）。
    """
    message = require_mapping(raw, path="exchangeInfo")
    symbols = require_field(message, "symbols", path="exchangeInfo")
    if isinstance(symbols, (str, bytes)) or not hasattr(symbols, "__iter__"):
        raise MarketDataFormatError("exchangeInfo.symbols: expected an array")

    quotes = frozenset(quote_assets)
    discovered: list[UniverseSymbol] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()

    for entry in symbols:
        item = require_mapping(entry, path="exchangeInfo.symbols[]")
        try:
            symbol = require_str(require_field(item, "symbol", path="symbol"), path="symbol")
            status = require_str(require_field(item, "status", path="status"), path="status")
            contract_type = require_str(
                require_field(item, "contractType", path="contractType"), path="contractType")
            quote = require_str(require_field(item, "quoteAsset", path="quoteAsset"),
                                path="quoteAsset")
            base = require_str(require_field(item, "baseAsset", path="baseAsset"), path="baseAsset")
        except MarketDataFormatError as exc:
            ident = str(item.get("symbol", "?")) if isinstance(item, dict) else "?"
            skipped.append((ident, f"malformed: {exc}"))
            continue

        if symbol in seen:
            skipped.append((symbol, "duplicate symbol in exchangeInfo"))
            continue
        reason = _skip_reason(status=status, contract_type=contract_type, quote=quote,
                              contract_types=contract_types, quotes=quotes)
        if reason is not None:
            skipped.append((symbol, reason))
            continue
        seen.add(symbol)
        discovered.append(UniverseSymbol(
            symbol=symbol, base_asset=base, quote_asset=quote,
            settlement_asset=_settlement_asset(item, fallback=quote),
            contract_type=contract_type, status=status))

    return Universe(discovered=tuple(discovered), skipped=tuple(skipped),
                    quote_assets=tuple(sorted(quotes)))


def _skip_reason(*, status: str, contract_type: str, quote: str,
                 contract_types: frozenset[str], quotes: frozenset[str]) -> str | None:
    if status != TRADING_STATUS:
        return f"status={status} (not TRADING)"
    if contract_type not in contract_types:
        return f"contractType={contract_type} (not a supported perpetual)"
    if quote not in quotes:
        return f"quoteAsset={quote} (not in allowed set)"
    return None


def _settlement_asset(item: object, *, fallback: str) -> str:
    """结算资产：优先 marginAsset/settleAsset，缺失回退 quote（USDⓈ-M 永续即 USDT）。"""
    if isinstance(item, dict):
        for key in ("marginAsset", "settleAsset"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return fallback


__all__ = [
    "DEFAULT_QUOTE_ASSETS",
    "TRADING_STATUS",
    "Universe",
    "UniverseSymbol",
    "parse_universe",
]
