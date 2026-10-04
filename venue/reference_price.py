"""ReferencePrice contract（P0001.15 §11–§12 + 人类裁决 1 A–E）。

纪律：

- 风险 / 会计路径**只接受** `InstrumentSpec.reference_price_policy.risk_price_types` 中的类型（当前 = `MARK`）；
- `MID` / `INDEX` / `LAST` 可以出现在 vocabulary / UI，但**不得静默替代** MARK；
- 没有正式来源 ⇒ `UNKNOWN` + reason（fail-closed），**不**回退到 last trade；
- 正式来源：`MarketEvent.MARK_PRICE`（Binance `markPriceUpdate` 归一化后的事件）。

时间语义：`as_of` = 交易所事件时间（venue 自己声明的有效期），`received_at` = 本地处理时间；
`freshness_ms = now_ms - as_of`。是否"过期"由 Risk 的既有 `max_mark_age_ms` 判定，本模块**不自造 TTL**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from domain.instruments.model import InstrumentSpec, PriceType
from market.events.types import EventType, MarketEvent, Milliseconds
from venue.identity import VenueIdentity

#: 缺少正式 MARK 来源时的 reason（fail-closed，UI/Assistant 直接展示）。
REFERENCE_PRICE_MARK_UNAVAILABLE = "REFERENCE_PRICE_MARK_UNAVAILABLE"
REFERENCE_PRICE_SOURCE_NOT_CONFIGURED = "REFERENCE_PRICE_SOURCE_NOT_CONFIGURED"
REFERENCE_PRICE_TYPE_NOT_ALLOWED_FOR_RISK = "REFERENCE_PRICE_TYPE_NOT_ALLOWED_FOR_RISK"
REFERENCE_PRICE_INSTRUMENT_UNKNOWN = "REFERENCE_PRICE_INSTRUMENT_UNKNOWN"


class ReferencePriceError(ValueError):
    """reference price 契约错误。"""


@dataclass(frozen=True, slots=True)
class ReferencePrice:
    """一次 reference price 事实（`known=False` ⇒ price/as_of/source 必须缺失 + reason 必填）。"""

    instrument_id: str
    price_type: PriceType
    known: bool
    price: float | None = None
    as_of: Milliseconds | None = None
    received_at: Milliseconds | None = None
    source: str | None = None
    freshness_ms: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id:
            raise ReferencePriceError("ReferencePrice.instrument_id must be a non-empty string")
        if not isinstance(self.price_type, PriceType):
            raise ReferencePriceError("ReferencePrice.price_type must be a PriceType")
        if not isinstance(self.known, bool):
            raise ReferencePriceError("ReferencePrice.known must be a bool")
        if self.known:
            if self.price is None or self.as_of is None or not self.source:
                raise ReferencePriceError(
                    "a known ReferencePrice requires price, as_of and source")
            if isinstance(self.price, bool) or not isinstance(self.price, (int, float)):
                raise ReferencePriceError("ReferencePrice.price must be a number")
            number = float(self.price)
            if not math.isfinite(number) or number <= 0.0:
                raise ReferencePriceError(f"ReferencePrice.price must be positive and finite, got {self.price!r}")
            object.__setattr__(self, "price", number)
            if self.reason is not None:
                raise ReferencePriceError("a known ReferencePrice must not carry an unknown reason")
        else:
            if self.price is not None or self.as_of is not None:
                raise ReferencePriceError(
                    "an unknown ReferencePrice must not carry a price or as_of (UNKNOWN is not a value)")
            if not self.reason:
                raise ReferencePriceError("an unknown ReferencePrice must carry a reason")

    @classmethod
    def unknown(cls, *, instrument_id: str, price_type: PriceType, reason: str) -> "ReferencePrice":
        return cls(instrument_id=instrument_id, price_type=price_type, known=False, reason=reason)

    @property
    def is_stale(self) -> bool | None:
        """`freshness_ms` 缺失 ⇒ `None`（UNKNOWN，不是"不过期"）。"""
        return None if self.freshness_ms is None else self.freshness_ms > 0

    def view(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "price_type": self.price_type.value,
            "known": self.known,
            "price": self.price,
            "as_of": self.as_of,
            "received_at": self.received_at,
            "source": self.source,
            "freshness_ms": self.freshness_ms,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MarkPriceFact:
    """由正式 `MarketEvent.MARK_PRICE` 归一化得到的 MARK 事实。"""

    venue_id: str
    instrument_id: str
    price: float
    exchange_ts: Milliseconds
    received_at: Milliseconds
    source: str

    def __post_init__(self) -> None:
        for name in ("venue_id", "instrument_id", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ReferencePriceError(f"MarkPriceFact.{name} must be a non-empty string")
        if isinstance(self.price, bool) or not isinstance(self.price, (int, float)):
            raise ReferencePriceError("MarkPriceFact.price must be a number")
        number = float(self.price)
        if not math.isfinite(number) or number <= 0.0:
            raise ReferencePriceError(f"MarkPriceFact.price must be positive and finite, got {self.price!r}")
        object.__setattr__(self, "price", number)
        for name in ("exchange_ts", "received_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReferencePriceError(f"MarkPriceFact.{name} must be a non-negative int epoch-ms")


class MarkPriceReferenceSource:
    """MARK source：只能由正式 `MarketEvent.MARK_PRICE` 推进（人类裁决 1A/1C）。

    绝不接受 last trade / mid 冒充 MARK：`on_market_event` 只认 `EventType.MARK_PRICE`。
    """

    #: 该 source 输出的 price type（结构性固定）
    price_type: PriceType = PriceType.MARK

    def __init__(self, *, venue_identity: VenueIdentity, instrument_id: str,
                 source: str = "market_event.mark_price") -> None:
        if not isinstance(instrument_id, str) or not instrument_id:
            raise ReferencePriceError("MarkPriceReferenceSource.instrument_id must be a non-empty string")
        self._venue = venue_identity
        self._instrument_id = instrument_id
        self._source = source
        self._latest: MarkPriceFact | None = None
        self._observation_count = 0

    @property
    def source_id(self) -> str:
        return f"{self._venue.venue_id}:{self._source}"

    @property
    def _source_name(self) -> str:
        return self._source

    @property
    def venue_id(self) -> str:
        return self._venue.venue_id

    @property
    def instrument_id(self) -> str:
        return self._instrument_id

    @property
    def observation_count(self) -> int:
        return self._observation_count

    def on_market_event(self, event: MarketEvent) -> bool:
        """消费一个统一市场事件；只有 `MARK_PRICE` 会推进 MARK（返回是否推进）。"""
        if not isinstance(event, MarketEvent):
            raise ReferencePriceError("on_market_event requires a MarketEvent")
        if event.event_type is not EventType.MARK_PRICE:
            return False
        payload = event.payload
        price = getattr(payload, "price", None)
        if price is None:
            raise ReferencePriceError("MARK_PRICE event payload must carry price")
        self.observe_mark_price(price=float(price), exchange_ts=int(event.exchange_ts),
                                received_at=int(event.process_ts))
        return True

    def observe_mark_price(self, *, price: float, exchange_ts: Milliseconds, received_at: Milliseconds,
                           source: str | None = None) -> MarkPriceFact:
        """记录一次正式 MARK 观测（供 venue 侧观测路径复用同一事实链）。"""
        fact = MarkPriceFact(venue_id=self._venue.venue_id, instrument_id=self._instrument_id,
                             price=float(price), exchange_ts=int(exchange_ts),
                             received_at=int(received_at), source=source or self._source)
        self._latest = fact
        self._observation_count += 1
        return fact

    def latest(self, *, now_ms: Milliseconds) -> ReferencePrice:
        fact = self._latest
        if fact is None:
            return ReferencePrice.unknown(instrument_id=self._instrument_id, price_type=PriceType.MARK,
                                          reason=REFERENCE_PRICE_MARK_UNAVAILABLE)
        return ReferencePrice(
            instrument_id=self._instrument_id, price_type=PriceType.MARK, known=True, price=fact.price,
            as_of=fact.exchange_ts, received_at=fact.received_at, source=fact.source,
            freshness_ms=max(0, int(now_ms) - fact.exchange_ts))

    def latest_fact(self) -> MarkPriceFact | None:
        return self._latest


@dataclass
class ReferencePriceProvider:
    """按 instrument 组织的 reference price 汇总（只读消费）。

    `for_risk(...)` 是**唯一**允许进入风险 / 会计路径的入口：它只查 policy 允许的类型，
    缺失即 `UNKNOWN`，绝不回退到其它 price type（SC-11 / §C）。
    """

    venue_identity: VenueIdentity
    sources: dict[tuple[str, PriceType], object] = field(default_factory=dict)

    def register(self, instrument_id: str, price_type: PriceType, source: object) -> None:
        if not isinstance(price_type, PriceType):
            raise ReferencePriceError("price_type must be a PriceType")
        key = (instrument_id, price_type)
        if key in self.sources:
            raise ReferencePriceError(f"reference price source already registered for {key}")
        self.sources[key] = source

    def observe_market_event(self, event: MarketEvent) -> int:
        """把统一市场事件分发给所有 source；返回被推进的 source 数量。"""
        advanced = 0
        for source in self.sources.values():
            handler = getattr(source, "on_market_event", None)
            if handler is not None and handler(event):
                advanced += 1
        return advanced

    def latest(self, instrument: InstrumentSpec, *, now_ms: Milliseconds,
               price_type: PriceType | None = None) -> ReferencePrice:
        if not isinstance(instrument, InstrumentSpec):
            raise ReferencePriceError("latest requires an InstrumentSpec")
        wanted = price_type or instrument.reference_price_policy.risk_price_type
        source = self.sources.get((instrument.instrument_id, wanted))
        if source is None:
            return ReferencePrice.unknown(instrument_id=instrument.instrument_id, price_type=wanted,
                                          reason=REFERENCE_PRICE_SOURCE_NOT_CONFIGURED)
        return source.latest(now_ms=int(now_ms))

    def for_risk(self, instrument: InstrumentSpec, *, now_ms: Milliseconds) -> ReferencePrice:
        """风险 / 会计路径入口：只接受 policy 允许的类型（`LAST` 结构性不可用）。"""
        policy = instrument.reference_price_policy
        price_type = policy.risk_price_type
        if price_type is PriceType.LAST:  # pragma: no cover - policy 构造期已禁止
            return ReferencePrice.unknown(instrument_id=instrument.instrument_id, price_type=price_type,
                                          reason=REFERENCE_PRICE_TYPE_NOT_ALLOWED_FOR_RISK)
        return self.latest(instrument, now_ms=now_ms, price_type=price_type)


__all__ = [
    "REFERENCE_PRICE_INSTRUMENT_UNKNOWN", "REFERENCE_PRICE_MARK_UNAVAILABLE",
    "REFERENCE_PRICE_SOURCE_NOT_CONFIGURED", "REFERENCE_PRICE_TYPE_NOT_ALLOWED_FOR_RISK", "MarkPriceFact",
    "MarkPriceReferenceSource", "ReferencePrice", "ReferencePriceError", "ReferencePriceProvider",
]
