"""产品层类型（P0001.10 §1 / §3 / §4）。

纪律（提案 §设计）：

- **产品层不拥有交易事实**（`Product layer does not own trading truth`）：这里只做事实搬运与装箱；
- **UNKNOWN 必须显式**：任何缺失事实都用 `Fact.unknown(reason)` 表达，绝不用 0 / 空列表 / healthy 顶替；
- 类型只描述"可展示的事实"，不含任何业务判断、阈值或重算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from market.events.types import Milliseconds

#: 产品 schema 版本（API 契约版本；内部 Python 类型不得裸序列化）
#: F-08：TraceEntry 增加 ts / identity_kind / latency_ms ⇒ 3 → 4；
#: Closure Slice 5：snapshot 新增 `ops` 段（network/auth/logging/retention/liveness）⇒ 4 → 5
SCHEMA_VERSION = "5"

#: 未知原因码（用于 `Fact.reason`）
UNKNOWN_NOT_PROVIDED = "not_provided"
UNKNOWN_NOT_AVAILABLE = "not_available_yet"


class RuntimeMode(Enum):
    """运行模式：UI/API 不得猜测当前模式（提案 §3）。"""

    REPLAY = "REPLAY"
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class Fact:
    """一个可展示的事实：**known/unknown 是一等字段**。

    `value is None` 且 `known=False` ⇒ 未知（必须带 `reason`）。
    未知**不等于** 0 / 空 / healthy。
    """

    known: bool
    value: object | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.known, bool):
            raise TypeError("Fact.known must be a bool")
        if not self.known and self.reason is None:
            raise ValueError("unknown Fact must carry a reason (unknown != 0/empty/healthy)")

    @classmethod
    def of(cls, value: object | None, *, unknown_reason: str = UNKNOWN_NOT_PROVIDED) -> "Fact":
        if value is None:
            return cls(known=False, value=None, reason=unknown_reason)
        return cls(known=True, value=value)

    @classmethod
    def unknown(cls, reason: str) -> "Fact":
        return cls(known=False, value=None, reason=reason)


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """运行实例身份（每个 API 响应都必须带，提案 §3）。"""

    mode: RuntimeMode
    environment: str
    venue: str
    symbol: str
    runtime_id: str
    started_at: Milliseconds
    data_timestamp: Fact

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode):
            raise TypeError("RuntimeIdentity.mode must be a RuntimeMode")
        for name in ("environment", "venue", "symbol", "runtime_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"RuntimeIdentity.{name} must be a non-empty string")
        if isinstance(self.started_at, bool) or not isinstance(self.started_at, int):
            raise TypeError("RuntimeIdentity.started_at must be an int (ms)")
        if not isinstance(self.data_timestamp, Fact):
            raise TypeError("RuntimeIdentity.data_timestamp must be a Fact")


@dataclass(frozen=True, slots=True)
class ConfigEntryView:
    """配置条目视图：**只有非敏感值 + secret 引用名**（P0001.11 §1）。"""

    name: str
    source: str
    value: Fact
    secret_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigView:
    """配置来源视图（`config_id` / `fingerprint` / 来源统计 / secret 引用清单）。"""

    config_id: Fact
    fingerprint: Fact
    created_at: Fact
    sources: dict[str, int] = field(default_factory=dict)
    secret_refs: tuple[str, ...] = ()
    entries: tuple[ConfigEntryView, ...] = ()


class BlockerOwner(Enum):
    """blocker 责任 Owner（不是优先级；裁决 D）。"""

    READINESS = "READINESS"
    RISK = "RISK"
    STRATEGY = "STRATEGY"
    ORCHESTRATOR = "ORCHESTRATOR"
    MARKET = "MARKET"
    PREDICTION = "PREDICTION"
    #: P0001.13：执行安全与交易所限额
    EXECUTION = "EXECUTION"
    VENUE = "VENUE"


class BlockerSeverity(Enum):
    """严重度三档（裁决 D）。"""

    BLOCKING = "BLOCKING"
    DEGRADED = "DEGRADED"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class BlockerView:
    """统一 blocker 视图（P0001.11 §4；字段由人类裁决固定）。"""

    owner: BlockerOwner
    reason_code: str
    severity: BlockerSeverity
    message: str
    source_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner, BlockerOwner):
            raise TypeError("BlockerView.owner must be a BlockerOwner")
        if not isinstance(self.severity, BlockerSeverity):
            raise TypeError("BlockerView.severity must be a BlockerSeverity")
        for name in ("reason_code", "message", "source_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"BlockerView.{name} must be a non-empty string")

    @property
    def key(self) -> str:
        """去重键 = owner + reason_code + source_ref（裁决 D）。"""
        return f"{self.owner.value}|{self.reason_code}|{self.source_ref}"


@dataclass(frozen=True, slots=True)
class ExecutionSafetyView:
    """执行安全投影（P0001.13；未接线 ⇒ 全部 UNKNOWN，绝不 green）。"""

    health_status: Fact = field(default_factory=lambda: Fact.unknown("execution safety not wired"))
    health_reasons: tuple[str, ...] = ()
    request_budget: Fact = field(default_factory=lambda: Fact.unknown("execution safety not wired"))
    order_budget: Fact = field(default_factory=lambda: Fact.unknown("execution safety not wired"))
    venue_limits: Fact = field(default_factory=lambda: Fact.unknown("execution safety not wired"))
    latency: Fact = field(default_factory=lambda: Fact.unknown("execution safety not wired"))
    reconciliation: Fact = field(default_factory=lambda: Fact.unknown("execution safety not wired"))
    anomalies: Fact = field(default_factory=lambda: Fact.unknown("execution safety not wired"))


@dataclass(frozen=True, slots=True)
class OpsView:
    """Operational posture（closure Slice 5 / F-12 / F-15）：四层健康 + network/logging/retention。

    四层语义**互不替代**：`process_live` 只回答"进程活着"，不代表可交易。
    """

    process_live: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    runtime_state: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    runtime_detail: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    trade_readiness: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    trade_readiness_reasons: tuple[str, ...] = ()
    execution_health: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    operational_warning: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    network: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    logging: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    retention: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))
    ts: Fact = field(default_factory=lambda: Fact.unknown("ops posture not wired"))


@dataclass(frozen=True, slots=True)
class InstrumentView:
    """Instrument identity + product semantics（P0001.15 §1–§5、§21）。"""

    instrument_id: Fact
    symbol: Fact
    asset_class: Fact
    product_type: Fact
    base_asset: Fact
    quote_asset: Fact
    settlement_asset: Fact
    price_tick: Fact
    quantity_step: Fact
    min_quantity: Fact
    min_notional: Fact
    production_ready: Fact
    capabilities: Fact = field(default_factory=lambda: Fact.unknown("no instrument capabilities"))
    reference_price_policy: Fact = field(default_factory=lambda: Fact.unknown("no reference price policy"))


@dataclass(frozen=True, slots=True)
class VenueView:
    """Venue identity（P0001.15 §6）：venue_id / venue_type / environment 三个正交维度。"""

    venue_id: Fact
    venue_type: Fact
    environment: Fact
    market_connector_id: Fact = field(default_factory=lambda: Fact.unknown("no market connector"))
    execution_connector_id: Fact = field(default_factory=lambda: Fact.unknown("no execution connector"))


@dataclass(frozen=True, slots=True)
class ReferencePriceView:
    """Reference price 事实（P0001.15 §11）：`known=False` ⇒ price/as_of/source 必须缺失 + reason 必填。"""

    instrument_id: Fact
    price_type: Fact
    known: Fact
    price: Fact
    as_of: Fact
    source: Fact
    freshness_ms: Fact
    reason: Fact


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """单个 connector 的健康（market / private 各自独立，P0001.15 §16–§18 / SC-28）。"""

    kind: str
    connector_id: Fact
    venue_id: Fact
    connection_state: Fact
    last_event_ms: Fact
    event_age_ms: Fact
    detail: Fact = field(default_factory=lambda: Fact.unknown("no connector detail"))
    observed: Fact = field(default_factory=lambda: Fact.unknown("no connector observation"))
    extras: Fact = field(default_factory=lambda: Fact.unknown("no connector extras"))


#: 未接线时的「未知」read-model 单例（immutable；供 snapshot 默认值复用，不伪造任何事实）
UNKNOWN_INSTRUMENT_VIEW = InstrumentView(
    instrument_id=Fact.unknown("no instrument"), symbol=Fact.unknown("no instrument"),
    asset_class=Fact.unknown("no instrument"), product_type=Fact.unknown("no instrument"),
    base_asset=Fact.unknown("no instrument"), quote_asset=Fact.unknown("no instrument"),
    settlement_asset=Fact.unknown("no instrument"), price_tick=Fact.unknown("no instrument"),
    quantity_step=Fact.unknown("no instrument"), min_quantity=Fact.unknown("no instrument"),
    min_notional=Fact.unknown("no instrument"), production_ready=Fact.unknown("no instrument"))
UNKNOWN_VENUE_VIEW = VenueView(
    venue_id=Fact.unknown("no venue"), venue_type=Fact.unknown("no venue"),
    environment=Fact.unknown("no venue"))
UNKNOWN_REFERENCE_PRICE_VIEW = ReferencePriceView(
    instrument_id=Fact.unknown("no reference price"), price_type=Fact.unknown("no reference price"),
    known=Fact.unknown("no reference price"), price=Fact.unknown("no reference price"),
    as_of=Fact.unknown("no reference price"), source=Fact.unknown("no reference price"),
    freshness_ms=Fact.unknown("no reference price"), reason=Fact.unknown("no reference price"))
UNKNOWN_MARKET_CONNECTOR_HEALTH = ConnectorHealth(
    kind="market", connector_id=Fact.unknown("no market connector"),
    venue_id=Fact.unknown("no market connector"), connection_state=Fact.unknown("no market connector"),
    last_event_ms=Fact.unknown("no market connector"), event_age_ms=Fact.unknown("no market connector"))
UNKNOWN_PRIVATE_CONNECTOR_HEALTH = ConnectorHealth(
    kind="private", connector_id=Fact.unknown("no private connector"),
    venue_id=Fact.unknown("no private connector"),
    connection_state=Fact.unknown("no private connector"), last_event_ms=Fact.unknown("no private connector"),
    event_age_ms=Fact.unknown("no private connector"))


@dataclass(frozen=True, slots=True)
class MarketView:
    healthy: Fact
    tradeable: Fact
    window_coverage_ms: Fact
    best_bid: Fact
    best_ask: Fact
    spread_bps: Fact
    market_state_hash: Fact
    #: F2：盘口可信度（既有 `BookHealth` 原始值）与数据新鲜度（`book_age_ms`）；
    #: `healthy` 只由 `book_health == healthy` 推导，`tradeable` 是独立事实。
    book_health: Fact = field(default_factory=lambda: Fact.unknown("book_health is not available"))
    book_age_ms: Fact = field(default_factory=lambda: Fact.unknown("book age is not available"))


@dataclass(frozen=True, slots=True)
class PredictionView:
    request_id: Fact
    sequence: Fact
    provider: Fact
    model: Fact
    as_of: Fact
    expires_at: Fact
    latency_ms: Fact
    derived_confidence: Fact
    market_state_hash: Fact
    freshest: Fact
    #: closure Slice：多 horizon 未来收益分布（既有 `Prediction.future_return` 事实；无记录 ⇒ UNKNOWN）
    horizons: Fact = field(default_factory=lambda: Fact.unknown("no prediction record yet"))
    #: P0001.17：本地试验预测标记（provider=LOCAL_TRIAL ⇒ True；UI/Assistatn 必须标注，不得称真实模型）
    is_local_trial: Fact = field(default_factory=lambda: Fact.unknown("no prediction record yet"))


@dataclass(frozen=True, slots=True)
class StrategyView:
    at_ms: Fact
    mode: Fact
    detail: Fact
    blocked_by: Fact
    bid_action: Fact
    ask_action: Fact
    bid_price: Fact
    bid_quantity: Fact
    ask_price: Fact
    ask_quantity: Fact


@dataclass(frozen=True, slots=True)
class RiskView:
    kill_switch_mode: Fact
    max_position_qty: Fact
    max_position_notional: Fact
    max_open_order_exposure: Fact
    max_daily_loss: Fact
    max_drawdown_pct: Fact
    realized_pnl_today: Fact
    drawdown: Fact
    peak_equity: Fact
    rejects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OrderView:
    """订单视图：`decision_id` 提供到 MakerDecision 的回溯（SC-7）。"""

    client_order_id: str
    side: str
    status: str
    price: Fact
    quantity: Fact
    filled_quantity: Fact
    reduce_only: bool
    created_at: Milliseconds
    updated_at: Milliseconds
    decision_id: Fact
    uncertain: bool
    #: P0001.15 §15 / SC-26–SC-27：来自订单自身的 canonical correlation（不是 runtime 侧映射）
    instrument_id: Fact = field(default_factory=lambda: Fact.unknown("order carries no instrument identity"))
    venue_id: Fact = field(default_factory=lambda: Fact.unknown("order carries no venue identity"))
    prediction_id: Fact = field(default_factory=lambda: Fact.unknown("order carries no prediction identity"))
    #: §25：venue 侧订单号（外部事实；PAPER 未分配 external id ⇒ UNKNOWN，不伪造）
    venue_order_id: Fact = field(default_factory=lambda: Fact.unknown("order carries no venue order id"))


@dataclass(frozen=True, slots=True)
class FillView:
    """成交事实视图（G1）：只搬运 FillLedger / execution 事实，不做任何推断。"""

    client_order_id: Fact
    ts: Fact
    price: Fact
    quantity: Fact
    fee: Fact
    trade_id: Fact


@dataclass(frozen=True, slots=True)
class ExecutionView:
    active_orders: tuple[OrderView, ...]
    uncertain_exposure: Fact
    open_order_exposure: Fact
    has_unknown_exposure: bool
    unknown_submit_count: Fact
    unknown_cancel_count: Fact
    #: G1：最近成交（有界；空元组表示"未接线/未提供"，不是"没有成交"——由 counts 区分）
    recent_fills: tuple[FillView, ...] = ()
    #: G1：成交上限（0 = 未接线，显式表达"不暴露成交"）
    recent_fill_limit: int = 0
    #: P0001.16 §16：最近一次 submit 的三分类与**脱敏后**的真实原因（UNKNOWN 必须可解释）
    last_submit_classification: Fact = field(
        default_factory=lambda: Fact.unknown("no submit has been attempted"))
    last_submit_reason: Fact = field(
        default_factory=lambda: Fact.unknown("no submit has been attempted"))


@dataclass(frozen=True, slots=True)
class PortfolioView:
    position_qty: Fact
    average_entry_price: Fact
    mark_price: Fact
    unrealized_pnl: Fact
    realized_pnl: Fact
    fees_paid: Fact
    funding_paid: Fact
    balance: Fact
    equity: Fact


@dataclass(frozen=True, slots=True)
class ReadinessView:
    status: Fact
    scope: Fact
    reasons: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    #: live readiness 是否适用于当前 mode（PAPER/REPLAY ⇒ False：只记录，不作为 submit authority）
    applicable: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))
    authority_id: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))
    #: G5：完整 authority 事实（kind / TTL / generation）
    authority_kind: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))
    authority_issued_at_ms: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))
    authority_expires_at_ms: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))
    authority_recovery_generation: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))
    authority_market_generation: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))


@dataclass(frozen=True, slots=True)
class HealthView:
    market_healthy: Fact
    private_stream_state: Fact
    clock_offset_ms: Fact
    uptime_ms: Fact
    notes: tuple[str, ...] = ()
    #: G4：prediction provider 状态（HEALTHY / DEGRADED / BACKING_OFF）与 accounting 健康
    prediction_provider: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))
    accounting: Fact = field(default_factory=lambda: Fact.unknown("not_provided"))
    #: F2：与 Monitor 同一 Market projection（System → Health 不再另外判健康）
    book_health: Fact = field(default_factory=lambda: Fact.unknown("book_health is not available"))
    market_tradeable: Fact = field(default_factory=lambda: Fact.unknown("tradeable is not available"))
    #: closure Slice 1 / F-11：runtime / loop 状态（STARTING/RUNNING/STOPPING/STOPPED/FAILED）
    runtime_state: Fact = field(default_factory=lambda: Fact.unknown("runtime state not provided"))
    runtime_detail: Fact = field(default_factory=lambda: Fact.unknown("runtime state not provided"))
    runtime_quoting: Fact = field(default_factory=lambda: Fact.unknown("runtime state not provided"))
    runtime_run_id: Fact = field(default_factory=lambda: Fact.unknown("runtime state not provided"))
    runtime_since_ms: Fact = field(default_factory=lambda: Fact.unknown("runtime state not provided"))


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """结构化因果链的一环（F-08）：只搬运既有 reason code / canonical identity。

    - `ts`：该事实发生时间（UNKNOWN 表示该阶段没有事实 ⇒ 排在已定时序之后）；
    - `identity_kind`：`identity` 是哪一类 canonical id（`client_order_id` / `fill_id` / ...）；
    - `latency_ms`：仅 ack 阶段使用（来自 Slice 3 已接的真实 observer）。
    """

    stage: str
    identity: Fact
    outcome: str
    reason_code: Fact
    detail: str = ""
    ts: Fact = field(default_factory=lambda: Fact.unknown("stage has no timestamp"))
    identity_kind: str = ""
    latency_ms: Fact = field(default_factory=lambda: Fact.unknown("no latency fact at this stage"))
    #: P0001.15 §24：causal chain 每个阶段都带同一 instrument / venue identity
    instrument_id: Fact = field(default_factory=lambda: Fact.unknown("no instrument identity at this stage"))
    venue_id: Fact = field(default_factory=lambda: Fact.unknown("no venue identity at this stage"))


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """因果链视图：回答"为什么没下单 / 为什么这个价格 / 为什么被拒 / 为什么 blocked"。"""

    trace: tuple[TraceEntry, ...] = ()
    readiness_blockers: tuple[str, ...] = ()
    risk_rejects: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
