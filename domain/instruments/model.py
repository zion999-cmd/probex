"""Instrument Domain（P0001.15 §1–§5）：描述「交易的是什么」。

边界（人类裁决 3）：

- 本模块只描述**事实**（identity / asset class / product type / capabilities / declared parameters /
  reference-price policy）；
- **不**参与风险数学，**不**修改 `RiskGate` / Readiness 规则；
- product semantics **不得**由 venue 名称推导。

与 venue rules 的分工（§5）：

- instrument semantics（本模块）：`PERPETUAL` / `has_funding` / `supports_short` / `settlement_asset` / 到期语义；
- venue rules（`TradingRules` / `venue.rules.*` config）：tick size / quantity step / min notional / 支持的订单类型 /
  rate limits / venue 专属限制 —— **执行时以 venue rules 为准**，本模块的 declared 参数只是 instrument 自身的声明，
  可用 `InstrumentSpec.venue_rules_discrepancies(rules)` 只读比对（返回差异事实，不抛错、不自动覆盖）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from domain.instruments.capabilities import (CRYPTO_PERPETUAL_CAPABILITIES, InstrumentCapabilities,
                                             SettlementType, TradingSessionType)


class InstrumentError(ValueError):
    """instrument 契约错误。"""


class AssetClass(Enum):
    """资产类别（§2）。P0001.15 只有 `CRYPTO` 具有生产实现。"""

    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    CRYPTO = "CRYPTO"


class ProductType(Enum):
    """产品类型（§3）。P0001.15 只有 `PERPETUAL` 具有生产实现。"""

    EQUITY_CASH = "EQUITY_CASH"
    SPOT = "SPOT"
    PERPETUAL = "PERPETUAL"
    DELIVERY_FUTURE = "DELIVERY_FUTURE"


class PriceType(Enum):
    """价格语义（§11）。`LAST` **永不**可作为风险 / 会计的 reference price。"""

    MARK = "MARK"
    MID = "MID"
    INDEX = "INDEX"
    LAST = "LAST"


#: 本阶段具有生产实现的 asset class / product type（其余仅为 vocabulary，SC-19）。
PRODUCTION_ASSET_CLASSES: frozenset[AssetClass] = frozenset({AssetClass.CRYPTO})
PRODUCTION_PRODUCT_TYPES: frozenset[ProductType] = frozenset({ProductType.PERPETUAL})

#: asset class 与 product type 的定义性匹配（不含业务数值）。
_PRODUCT_TYPES_BY_ASSET_CLASS: dict[AssetClass, frozenset[ProductType]] = {
    AssetClass.EQUITY: frozenset({ProductType.EQUITY_CASH}),
    AssetClass.FUTURE: frozenset({ProductType.DELIVERY_FUTURE}),
    AssetClass.CRYPTO: frozenset({ProductType.SPOT, ProductType.PERPETUAL, ProductType.DELIVERY_FUTURE}),
}


@dataclass(frozen=True, slots=True)
class ReferencePricePolicy:
    """哪些 `PriceType` 可以作为风险 / 会计的 reference price（§11 / §12 + 人类裁决 1）。

    - `risk_price_types`：可用于风险 / 会计路径的类型。**`LAST` 被结构性禁止**（既有 `AccountingCore`
      契约即「不使用 last trade」；human ruling：不得用 last trade 冒充 mark price）。
    - `observable_price_types`：允许出现在 vocabulary / UI（可被展示与解释），但**不得**静默替代
      `risk_price_types` 中的类型。
    """

    risk_price_types: tuple[PriceType, ...] = (PriceType.MARK,)
    observable_price_types: tuple[PriceType, ...] = (
        PriceType.MARK, PriceType.MID, PriceType.INDEX, PriceType.LAST,
    )

    def __post_init__(self) -> None:
        for name in ("risk_price_types", "observable_price_types"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not value:
                raise InstrumentError(f"ReferencePricePolicy.{name} must be a non-empty tuple")
            for item in value:
                if not isinstance(item, PriceType):
                    raise InstrumentError(f"ReferencePricePolicy.{name} must contain PriceType values")
            if len(set(value)) != len(value):
                raise InstrumentError(f"ReferencePricePolicy.{name} must not contain duplicates")
        if PriceType.LAST in self.risk_price_types:
            raise InstrumentError(
                "ReferencePricePolicy.risk_price_types must never contain LAST "
                "(last trade may not be substituted for a formal mark price)")
        unknown = set(self.risk_price_types) - set(self.observable_price_types)
        if unknown:
            raise InstrumentError(
                f"ReferencePricePolicy.risk_price_types must be observable as well, got {sorted(t.value for t in unknown)}")

    @property
    def risk_price_type(self) -> PriceType:
        """当前风险路径唯一接受的类型（本阶段固定为 `MARK`）。"""
        return self.risk_price_types[0]

    @property
    def allows_risk_substitution(self) -> bool:
        """是否会用一个 price type 去替代另一个（本阶段结构性为 `False`）。"""
        return len(self.risk_price_types) > 1

    def accepts_for_risk(self, price_type: PriceType) -> bool:
        return price_type in self.risk_price_types

    def view(self) -> dict[str, object]:
        return {
            "risk_price_types": [item.value for item in self.risk_price_types],
            "observable_price_types": [item.value for item in self.observable_price_types],
            "substitution_allowed": self.allows_risk_substitution,
        }


#: CRYPTO PERPETUAL 的 reference price policy：风险路径只接受正式 MARK（人类裁决 1）。
CRYPTO_PERPETUAL_REFERENCE_PRICE_POLICY = ReferencePricePolicy()


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """正式 Instrument identity + 业务语义（§1）。"""

    instrument_id: str
    symbol: str
    asset_class: AssetClass
    product_type: ProductType
    base_asset: str
    quote_asset: str
    settlement_asset: str
    price_tick: float
    quantity_step: float
    min_quantity: float
    min_notional: float
    capabilities: InstrumentCapabilities
    reference_price_policy: ReferencePricePolicy = CRYPTO_PERPETUAL_REFERENCE_PRICE_POLICY

    def __post_init__(self) -> None:
        for name in ("instrument_id", "symbol", "base_asset", "quote_asset", "settlement_asset"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise InstrumentError(f"InstrumentSpec.{name} must be a non-empty string")
        if not isinstance(self.asset_class, AssetClass):
            raise InstrumentError("InstrumentSpec.asset_class must be an AssetClass")
        if not isinstance(self.product_type, ProductType):
            raise InstrumentError("InstrumentSpec.product_type must be a ProductType")
        if not isinstance(self.capabilities, InstrumentCapabilities):
            raise InstrumentError("InstrumentSpec.capabilities must be InstrumentCapabilities")
        if not isinstance(self.reference_price_policy, ReferencePricePolicy):
            raise InstrumentError("InstrumentSpec.reference_price_policy must be a ReferencePricePolicy")
        for name in ("price_tick", "quantity_step", "min_quantity", "min_notional"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InstrumentError(f"InstrumentSpec.{name} must be a number")
            number = float(value)
            if not math.isfinite(number) or number <= 0.0:
                raise InstrumentError(f"InstrumentSpec.{name} must be a positive finite number, got {value!r}")
            object.__setattr__(self, name, number)
        allowed = _PRODUCT_TYPES_BY_ASSET_CLASS[self.asset_class]
        if self.product_type not in allowed:
            raise InstrumentError(
                f"product_type {self.product_type.value} is not valid for asset_class {self.asset_class.value}")
        if self.capabilities.has_expiry and self.product_type is ProductType.PERPETUAL:
            raise InstrumentError("a PERPETUAL instrument cannot have expiry")
        if self.product_type is ProductType.PERPETUAL and not self.capabilities.has_funding:
            raise InstrumentError("a PERPETUAL instrument must have funding")

    # ------------------------------------------------------------------ 派生事实

    @property
    def production_ready(self) -> bool:
        """本阶段是否具有生产实现（EQUITY / FUTURE 只是 vocabulary）。"""
        return (self.asset_class in PRODUCTION_ASSET_CLASSES
                and self.product_type in PRODUCTION_PRODUCT_TYPES)

    @property
    def is_derivative(self) -> bool:
        return self.product_type in (ProductType.PERPETUAL, ProductType.DELIVERY_FUTURE)

    def venue_rules_discrepancies(self, rules: Any) -> tuple[str, ...]:
        """与 venue rules（`TradingRules`）的只读比对；返回差异描述（不抛错、不覆盖）。

        instrument 的 declared 参数是 product 自身声明；**执行以 venue rules 为准**（§5）。
        """
        pairs = (("price_tick", "tick_size"), ("quantity_step", "step_size"),
                 ("min_quantity", "min_qty"), ("min_notional", "min_notional"))
        discrepancies: list[str] = []
        for spec_field, rules_field in pairs:
            declared = getattr(self, spec_field)
            actual = getattr(rules, rules_field, None)
            if actual is None:
                discrepancies.append(f"{spec_field}: venue rules do not provide {rules_field}")
                continue
            if not math.isclose(float(declared), float(actual), rel_tol=0.0, abs_tol=1e-12):
                discrepancies.append(
                    f"{spec_field}={declared!r} differs from venue {rules_field}={float(actual)!r}")
        return tuple(discrepancies)

    def view(self) -> dict[str, object]:
        """审计 / UI 用的扁平视图。"""
        return {
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "product_type": self.product_type.value,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "settlement_asset": self.settlement_asset,
            "price_tick": self.price_tick,
            "quantity_step": self.quantity_step,
            "min_quantity": self.min_quantity,
            "min_notional": self.min_notional,
            "production_ready": self.production_ready,
            "capabilities": self.capabilities.view(),
            "reference_price_policy": self.reference_price_policy.view(),
        }


def perpetual_crypto_spec(
    *,
    instrument_id: str,
    symbol: str,
    base_asset: str,
    quote_asset: str,
    settlement_asset: str,
    price_tick: float,
    quantity_step: float,
    min_quantity: float,
    min_notional: float,
    capabilities: InstrumentCapabilities = CRYPTO_PERPETUAL_CAPABILITIES,
    reference_price_policy: ReferencePricePolicy = CRYPTO_PERPETUAL_REFERENCE_PRICE_POLICY,
) -> InstrumentSpec:
    """构造一个 CRYPTO PERPETUAL spec（数值取自调用方 = venue rules / config，不在此处发明）。"""
    return InstrumentSpec(
        instrument_id=instrument_id, symbol=symbol, asset_class=AssetClass.CRYPTO,
        product_type=ProductType.PERPETUAL, base_asset=base_asset, quote_asset=quote_asset,
        settlement_asset=settlement_asset, price_tick=price_tick, quantity_step=quantity_step,
        min_quantity=min_quantity, min_notional=min_notional, capabilities=capabilities,
        reference_price_policy=reference_price_policy)


def instrument_id_for(symbol: str, *, venue_id: str) -> str:
    """canonical instrument id：`<venue_id>:<symbol>`（venue 归属是 identity 的一部分）。"""
    if not isinstance(symbol, str) or not symbol:
        raise InstrumentError("symbol must be a non-empty string")
    if not isinstance(venue_id, str) or not venue_id:
        raise InstrumentError("venue_id must be a non-empty string")
    return f"{venue_id}:{symbol}"


__all__ = [
    "PRODUCTION_ASSET_CLASSES", "PRODUCTION_PRODUCT_TYPES", "CRYPTO_PERPETUAL_REFERENCE_PRICE_POLICY",
    "AssetClass", "InstrumentError", "InstrumentSpec", "PriceType", "ProductType",
    "ReferencePricePolicy", "TradingSessionType", "SettlementType", "instrument_id_for",
    "perpetual_crypto_spec",
]
