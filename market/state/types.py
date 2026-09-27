"""MarketState：不可变市场状态快照（Feature Schema v1）。

`MarketState` 是「在时刻 t 系统知道什么」的完整答案。它在构造后不可修改，
Jev 等下游拿到的永远是某一个确定的 `MarketState(t)`，而不是一个正在变化的全局对象。

缺失值语义：未知 ≠ 0。任何无法计算或数据不足的 feature 一律为 `None`，
`0.0` 只代表「确实观测到数值为 0」。
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import Milliseconds, Venue
from market.state.quality import DataQuality

#: Feature Schema 版本。
#:
#: 任一 feature 的公式、单位、窗口或缺失值语义发生变化，都必须升级该版本号
#: （参见 proposals/P0001.3 §11）。
FEATURE_SCHEMA_VERSION = "market-state-v1"


@dataclass(frozen=True, slots=True)
class MarketIdentity:
    """状态所描述的市场标的。"""

    venue: Venue
    symbol: str


@dataclass(frozen=True, slots=True)
class StateTime:
    """状态的时间与水印。

    `event_ordinal` 是本引擎消费事件的 0 基递增计数器（确定性，与 Live/Replay 无关）。
    """

    as_of_exchange_ts: Milliseconds
    as_of_receive_ts: Milliseconds
    event_ordinal: int


@dataclass(frozen=True, slots=True)
class PriceFeatures:
    """最优档与价格派生量。"""

    best_bid: float | None
    best_ask: float | None
    bid_size: float | None
    ask_size: float | None
    mid: float | None
    spread: float | None
    spread_bps: float | None
    microprice: float | None


@dataclass(frozen=True, slots=True)
class DepthFeatures:
    """多档深度与失衡。档位在 schema 中固定为 1 / 5 / 10 / 20。"""

    bid_depth_1: float | None
    bid_depth_5: float | None
    bid_depth_10: float | None
    bid_depth_20: float | None
    ask_depth_1: float | None
    ask_depth_5: float | None
    ask_depth_10: float | None
    ask_depth_20: float | None
    l1_imbalance: float | None
    depth_imbalance_1: float | None
    depth_imbalance_5: float | None
    depth_imbalance_10: float | None
    depth_imbalance_20: float | None
    #: VAMP_5：双边前 5 档的成交量加权中间价（来自盘口，非成交 VWAP）。
    vamp: float | None


@dataclass(frozen=True, slots=True)
class FlowFeatures:
    """事件域订单流。计数为累计值，窗口量为滚动值。"""

    event_ofi: float | None
    ofi_1s: float | None
    ofi_5s: float | None
    ofi_15s: float | None
    normalized_ofi_1s: float | None
    normalized_ofi_5s: float | None
    normalized_ofi_15s: float | None
    book_update_count: int
    bid_update_count: int
    ask_update_count: int
    level_additions: int
    level_removals: int


@dataclass(frozen=True, slots=True)
class TradeFeatures:
    """成交域。本阶段尚无 Trade MarketEvent，全部为 unavailable。"""

    trade_stream_available: bool
    buy_aggressive_volume: float | None
    sell_aggressive_volume: float | None
    signed_volume: float | None
    cvd: float | None
    trade_count: int | None
    trade_intensity: float | None
    vwap: float | None


#: 本阶段没有真实 Trade MarketEvent，一律输出 unavailable（不伪造成交数据）。
UNAVAILABLE_TRADE_FEATURES = TradeFeatures(
    trade_stream_available=False,
    buy_aggressive_volume=None,
    sell_aggressive_volume=None,
    signed_volume=None,
    cvd=None,
    trade_count=None,
    trade_intensity=None,
    vwap=None,
)


@dataclass(frozen=True, slots=True)
class ReturnsFeatures:
    """多时间尺度的 mid 收益。"""

    return_1s: float | None
    return_3s: float | None
    return_5s: float | None
    return_15s: float | None
    return_30s: float | None
    return_60s: float | None
    return_300s: float | None


@dataclass(frozen=True, slots=True)
class VolatilityFeatures:
    """每秒已实现波动率（量纲 1/√s）。"""

    realized_volatility_5s: float | None
    realized_volatility_15s: float | None
    realized_volatility_30s: float | None
    realized_volatility_60s: float | None


@dataclass(frozen=True, slots=True)
class MarketState:
    """不可变市场状态。"""

    identity: MarketIdentity
    time: StateTime
    quality: DataQuality
    price: PriceFeatures
    depth: DepthFeatures
    flow: FlowFeatures
    trade: TradeFeatures
    returns: ReturnsFeatures
    volatility: VolatilityFeatures
    feature_schema_version: str

    def __post_init__(self) -> None:
        if self.feature_schema_version != FEATURE_SCHEMA_VERSION:
            raise ValueError(
                f"feature_schema_version must be {FEATURE_SCHEMA_VERSION!r}, "
                f"got {self.feature_schema_version!r}"
            )
