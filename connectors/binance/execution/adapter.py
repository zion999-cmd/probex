"""`BinanceExecutionAdapter`：第一个真实 Binance USDⓈ-M 写执行适配器（P0001.9.6）。

职责**只有**两件（§1）：

```text
Order            → Binance wire request（LIMIT + GTX + BOTH）
Binance 事实      → ExecutionEvent（供既有 OrderTracker / Accounting 消费）
```

它**不做**任何交易决策：方向、Jev 概率、maker edge、仓位目标、风险限额都仍在上层
（静态测试固定：本模块不 import `risk` / `strategy` / `prediction` / `jev`）。

关键不变量：

1. **submit 前必须验证 authority**（§2 / SC-1 / SC-19）：验证失败 ⇒ **不发送任何 HTTP 请求**；
2. **cancel 不要求 LIVE_READY**（降险动作不可被锁死，§2 / SC-20），但仍要求"目标订单属于 Probex + scope 明确 + 凭据可用"；
3. **三分类结果**（§5）：只有交易所明确 ACK ⇒ `OrderAccepted`；明确业务拒绝 ⇒ `OrderRejected`；
   其余（超时/断连/5xx/不可解析）⇒ UNKNOWN ⇒ **返回空事件**（上层按 uncertain exposure 处理）且**绝不自动重试**（§6）；
4. **clientOrderId 幂等主键**：直接使用 `Order.client_order_id` 作为 `newClientOrderId`（§4）；
5. **第一版只允许 LIMIT + GTX + BOTH**；`post_only=False` **本地拒绝**（§12）；不自动 round（§14）；
6. **user stream 是主要异步事实源**（§9）：`bridge_user_event()` 把 `ORDER_TRADE_UPDATE` 转成现有
   `ExecutionEvent`，不另建订单状态机；REST ACK 与 stream 重复由既有 `OrderTracker` 去重（§10）；
7. **UNKNOWN 用 `query_order()` 收敛**（§7），依然不重试写请求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from market.events.types import Milliseconds, Venue

from execution.events import (
    ExecutionEvent,
    FillReceived,
    OrderAccepted,
    OrderCanceled,
    OrderExpired,
    OrderRejected,
    OrderStatusUpdate,
)
from execution.types import (
    ExternalFill,
    ExternalOrder,
    Order,
    OrderStatus,
    can_transition,
)
from readiness.authority import (
    AuthorityInvalidReason,
    ExecutionReadinessAuthority,
    ExecutionReadinessAuthorityValidator,
)
from execution.normalization import OrderNormalizer


def normalizer_from_rules(rules: TradingRules, *, price_rounding: str, quantity_rounding: str) -> OrderNormalizer:
    """由交易所 `exchangeInfo` 规则构造归一化器（P0001.9.7.2）。

    放在连接器层是刻意的：`execution/` 域不得依赖 `connectors/` 的交易所类型（layering 契约）。
    舍入模式仍必须由调用方**显式**给出（业务选择，无默认值）。
    """
    from decimal import Decimal

    if not isinstance(rules, TradingRules):
        raise ExecutionAdapterError("normalizer_from_rules requires TradingRules")
    return OrderNormalizer(
        tick_size=Decimal(str(rules.tick_size)),
        step_size=Decimal(str(rules.step_size)),
        price_rounding=price_rounding,
        quantity_rounding=quantity_rounding,
    )
from readiness.bootstrap import BootstrapAuthority, BootstrapWriteGate
from readiness.types import Environment, PrivateLatencyStatus, RecoveryGeneration
from risk.types import KillSwitchMode

from connectors.binance.execution.parsing import (
    ExecutionParseError,
    OrderQueryFacts,
    SubmitAcknowledgement,
    SubmitClassification,
    classify_submit_response,
    parse_order_query,
    parse_rejection,
    parse_submit_response,
)
from connectors.binance.execution.rest import (
    BinanceExecutionRestClient,
    ExecutionOutcomeUnknown,
    ExecutionRequestRejected,
    OrderSide,
)
from connectors.binance.market_data.errors import TransportError
from connectors.binance.market_data.exchange_info import TradingRules
from connectors.binance.private.events import OrderUpdateObservation
from connectors.binance.private.errors import PrivateApiError
from connectors.binance.private.orders import is_probex_order

#: 本地拒单原因前缀（审计用；**不是**交易所事实）。
LOCAL_REJECTION_PREFIX = "local_reject:"

#: Binance 订单状态 → 本地 `OrderStatus`（显式映射，未知即拒绝转换）。
_STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.OPEN,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
    "REJECTED": OrderStatus.FAILED,
}
#: 交易所明确终态（cancel 时若已是终态，按事实处理而不是假装撤单成功）。
_TERMINAL_EXCHANGE_STATUSES = frozenset({"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"})


class ExecutionAdapterError(PrivateApiError):
    """adapter 契约错误（本地拒绝 / 前置条件不满足）。"""


class ExternalFactsUnavailableError(ExecutionAdapterError):
    """**外部事实不可用**（provider 未注入 / 读取失败 / 解析失败）。

    这是 fail closed 的关键语义：**UNKNOWN ≠ EMPTY**。
    `open_orders() -> ()` 的含义是"交易所确认没有挂单"，绝不能用来表示"我们不知道"，
    否则 reconciliation 会把未知误当"外部无订单/无成交"，破坏 uncertain exposure 与 recovery 纪律。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"external facts unavailable: {detail}")
        self.detail = detail


class ExternalFactsProvider(Protocol):
    """外部事实 provider（由调用方注入；缺省即 unavailable，绝不静默为空）。"""

    def parse_open_orders(self, symbol: str) -> tuple[ExternalOrder, ...]:
        """返回**真实**外部挂单（无挂单时返回空元组；读取/解析失败必须抛错）。"""

    def parse_recent_fills(
        self, symbol: str, *, since_ms: Milliseconds | None = None
    ) -> tuple[ExternalFill, ...]:
        """返回**真实**近期成交（无成交时返回空元组；读取/解析失败必须抛错）。"""


class SubmitRefusedError(ExecutionAdapterError):
    """submit 被**本地**拒绝（未发送任何请求）；`reason` 为 reason code。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{LOCAL_REJECTION_PREFIX}{reason}" + (f" ({detail})" if detail else ""))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityContext:
    """写请求前置校验需要的**当前**事实（由调用方在写请求前即时提供）。

    P0001.9.7.1：`authority` 显式支持两种 kind：

    - `ExecutionReadinessAuthority`（NORMAL）：走原有的 `validator.validate(...)` 语义；
    - `BootstrapAuthority`（BOOTSTRAP）：走 `bootstrap_gate.authorize(...)`（更窄约束 + 额度消耗）。

    `bootstrap_gate` / `latency_status` 缺失时，BOOTSTRAP 路径一律拒绝（fail closed，不是放行）。
    """

    authority: ExecutionReadinessAuthority | BootstrapAuthority | None
    recovery_generation: RecoveryGeneration
    market_generation: int
    hwm_activation_id: str | None
    hwm_generation: int
    kill_switch_mode: KillSwitchMode
    #: BOOTSTRAP 专用：写边界闸门（owns the single write-attempt quota）
    bootstrap_gate: BootstrapWriteGate | None = None
    #: BOOTSTRAP 专用：当前 private latency 可测性状态（未提供 ⇒ 按 UNKNOWN 处理 ⇒ 拒绝）
    latency_status: PrivateLatencyStatus | None = None
    #: BOOTSTRAP 专用（JIT）：collect 到写请求之间 private continuity 是否仍有效
    #: （未提供 / False ⇒ 写边界拒绝；WS reconnect 后必须重新 recovery+readiness）
    private_continuity_valid: bool | None = None


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """一次 submit 的结构化结果（三分类 + 事实；**绝不**用 bool 表达）。"""

    classification: SubmitClassification
    client_order_id: str
    events: tuple[ExecutionEvent, ...] = ()
    acknowledgement: SubmitAcknowledgement | None = None
    rejection_code: int | None = None
    rejection_message: str = ""
    detail: str = ""


@dataclass
class BinanceExecutionAdapter:
    """真实写执行 adapter（实现 `ExecutionAdapter` 协议）。"""

    rest: BinanceExecutionRestClient
    environment: Environment
    authority_provider: Callable[[], ExecutionAuthorityContext]
    rules_provider: Callable[[], TradingRules | None]
    symbol: str
    venue: Venue = Venue.BINANCE
    validator: ExecutionReadinessAuthorityValidator = field(
        default_factory=ExecutionReadinessAuthorityValidator
    )
    #: 本地状态查询（用于把 stream 事实安全地映射成事件；由调用方注入 tracker 查询）
    local_status_provider: Callable[[str], OrderStatus | None] | None = None
    #: 外部事实 provider（reconciliation 输入）。**未注入 ⇒ unavailable（不是空）**。
    private_read: ExternalFactsProvider | None = None
    #: P0001.9.7.2：**显式注入**的订单参数归一化器（price/quantity 的 tick/step 量化）。
    #: 为 None 时 adapter 不做任何隐式 round（D-048 不变）；启用后 REST 请求只使用
    #: Decimal 精确字符串，不经过 float 往返。
    normalizer: OrderNormalizer | None = None
    _queue: list[ExecutionEvent] = field(default_factory=list)
    _submitted: dict[str, tuple[str, str]] = field(default_factory=dict)  # client_id -> (symbol, exchange_id)
    _skipped_transitions: int = 0
    _unknown_submits: int = 0
    _unknown_cancels: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.environment, Environment):
            raise ExecutionAdapterError("environment must be an Environment")
        if not callable(self.authority_provider):
            raise ExecutionAdapterError("authority_provider must be callable")
        if not callable(self.rules_provider):
            raise ExecutionAdapterError("rules_provider must be callable")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ExecutionAdapterError("symbol must be a non-empty string")

    # ------------------------------------------------------------------ 观测（telemetry）

    @property
    def unknown_submit_count(self) -> int:
        return self._unknown_submits

    @property
    def unknown_cancel_count(self) -> int:
        return self._unknown_cancels

    @property
    def skipped_transition_count(self) -> int:
        """被跳过的、会违反本地状态机的 stream 事实数量（交给 reconciliation 处理）。"""
        return self._skipped_transitions

    def known_exchange_order_id(self, client_order_id: str) -> str | None:
        entry = self._submitted.get(client_order_id)
        return None if entry is None else entry[1]

    # ------------------------------------------------------------------ ExecutionAdapter

    def submit(self, order: Order) -> tuple[ExecutionEvent, ...]:
        """提交订单；返回立即可确认的事件（UNKNOWN ⇒ **空元组**，§5 / §6）。"""
        outcome = self.submit_with_outcome(order)
        return outcome.events

    def submit_with_outcome(self, order: Order) -> SubmitOutcome:
        """与 `submit()` 相同，但返回结构化三分类（供上层审计与 uncertain 处理）。"""
        if not isinstance(order, Order):
            raise ExecutionAdapterError("submit() requires an Order")
        try:
            self._require_submittable(order)
        except SubmitRefusedError as refusal:
            event = OrderRejected(
                client_order_id=order.client_order_id,
                reason=f"{LOCAL_REJECTION_PREFIX}{refusal.reason}",
                timestamp=order.updated_at,
            )
            return SubmitOutcome(
                classification=SubmitClassification.CONFIRMED_REJECTED,
                client_order_id=order.client_order_id,
                events=(event,),
                rejection_message=refusal.reason,
                detail=refusal.detail,
            )

        # P0001.9.7.2：归一化（仅在**显式注入** normalizer 时）：REST 请求使用 Decimal 精确字符串。
        price: float | Decimal = order.price
        quantity: float | Decimal = order.quantity
        if self.normalizer is not None:
            normalized = self.normalizer.normalize(price=order.price, quantity=order.quantity)
            price = normalized.price
            quantity = normalized.quantity

        try:
            raw = self.rest.submit_post_only_limit(
                symbol=order.symbol,
                side=OrderSide(order.side.value.upper()),
                quantity=quantity,
                price=price,
                client_order_id=order.client_order_id,
                reduce_only=order.reduce_only,
            )
        except BaseException as error:  # noqa: BLE001 - 三分类必须覆盖一切异常（UNKNOWN 最常见）
            classification = classify_submit_response(error)
            if classification is SubmitClassification.CONFIRMED_REJECTED:
                rejection = parse_rejection(error, client_order_id=order.client_order_id)
                return SubmitOutcome(
                    classification=classification,
                    client_order_id=order.client_order_id,
                    events=(
                        OrderRejected(
                            client_order_id=order.client_order_id,
                            reason=f"exchange_reject:{rejection.code}",
                            timestamp=order.updated_at,
                        ),
                    ),
                    rejection_code=rejection.code,
                    rejection_message=rejection.message,
                )
            # UNKNOWN：不重试、不生成 FAILED；上层按 uncertain exposure 处理并 query 收敛
            self._unknown_submits += 1
            return SubmitOutcome(
                classification=SubmitClassification.UNKNOWN,
                client_order_id=order.client_order_id,
                detail=type(error).__name__,
            )

        try:
            acknowledgement = parse_submit_response(raw, client_order_id=order.client_order_id)
        except ExecutionParseError:
            # 响应不可解析 ⇒ 无法确认结果 ⇒ UNKNOWN（绝不猜成功/失败）
            self._unknown_submits += 1
            return SubmitOutcome(
                classification=SubmitClassification.UNKNOWN,
                client_order_id=order.client_order_id,
                detail="unparseable acknowledgement",
            )
        self._submitted[order.client_order_id] = (order.symbol, acknowledgement.exchange_order_id)
        return SubmitOutcome(
            classification=SubmitClassification.CONFIRMED_ACCEPTED,
            client_order_id=order.client_order_id,
            events=(
                OrderAccepted(
                    client_order_id=order.client_order_id,
                    exchange_order_id=acknowledgement.exchange_order_id,
                    timestamp=order.updated_at,
                ),
            ),
            acknowledgement=acknowledgement,
        )

    def cancel(self, order: Order) -> tuple[ExecutionEvent, ...]:
        """发送撤单请求（**不要求 LIVE_READY**，§2 / SC-20）；UNKNOWN ⇒ 空元组。"""
        if not isinstance(order, Order):
            raise ExecutionAdapterError("cancel() requires an Order")
        self._require_cancelable(order)
        try:
            raw = self.rest.cancel_order(symbol=order.symbol, client_order_id=order.client_order_id)
        except ExecutionOutcomeUnknown:
            self._unknown_cancels += 1
            return ()
        except ExecutionRequestRejected as rejection:
            # 交易所明确拒绝撤单（常见：已 FILLED / 已 CANCELED）⇒ 用 query 取事实，不假装成功
            if rejection.code in (-2011, -2013):  # Unknown order / Order does not exist
                return ()
            raise
        except (TransportError, TimeoutError, OSError):
            self._unknown_cancels += 1
            return ()

        try:
            facts = parse_order_query(raw, client_order_id=order.client_order_id)
        except ExecutionParseError:
            self._unknown_cancels += 1
            return ()
        return self._events_from_query(facts, timestamp=order.updated_at)

    def poll(self) -> tuple[ExecutionEvent, ...]:
        """取走自上次 poll 以来由 user stream / query 产生的执行事件。"""
        pending = tuple(self._queue)
        self._queue.clear()
        return pending

    def open_orders(self) -> tuple[ExternalOrder, ...]:
        """外部当前挂单（reconciliation 输入）：只返回 **Probex 自己的**订单。

        **UNKNOWN ≠ EMPTY**：provider 未注入或读取/解析失败 ⇒ 抛 `ExternalFactsUnavailableError`，
        绝不返回空元组（空元组的语义只能是"交易所确认没有挂单"）。
        """
        provider = self._require_provider("open_orders")
        try:
            parsed = provider.parse_open_orders(self.symbol)
        except ExternalFactsUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任何失败都必须传播为 unavailable，不得降级为空
            raise ExternalFactsUnavailableError(f"open_orders failed: {type(exc).__name__}") from exc
        return tuple(order for order in parsed if is_probex_order(order.client_order_id))

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]:
        """外部近期成交（reconciliation 输入）。

        **UNKNOWN ≠ EMPTY**：provider 未注入或读取/解析失败 ⇒ 抛 `ExternalFactsUnavailableError`。
        归属不明的成交不由本方法擅自认领（由 recovery/reconciliation 的完整窗口负责）。
        """
        provider = self._require_provider("recent_fills")
        try:
            fills = provider.parse_recent_fills(self.symbol, since_ms=since_ms)
        except ExternalFactsUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExternalFactsUnavailableError(f"recent_fills failed: {type(exc).__name__}") from exc
        return tuple(fills)

    def _require_provider(self, method: str) -> ExternalFactsProvider:
        provider = self.private_read
        if provider is None:
            raise ExternalFactsUnavailableError(
                f"{method} requires an injected external-facts provider; "
                "missing provider means UNKNOWN, not EMPTY"
            )
        for name in ("parse_open_orders", "parse_recent_fills"):
            if not callable(getattr(provider, name, None)):
                raise ExternalFactsUnavailableError(f"{method}: provider lacks {name}()")
        return provider

    # ------------------------------------------------------------------ uncertain 收敛（§7）

    def query_order(self, *, client_order_id: str, timestamp: Milliseconds) -> tuple[ExecutionEvent, ...]:
        """按 `origClientOrderId` 查询并返回事实事件（UNKNOWN 时**不**需要 orderId）。"""
        try:
            raw = self.rest.query_order(symbol=self.symbol, client_order_id=client_order_id)
        except ExecutionOutcomeUnknown:
            return ()
        except (TransportError, TimeoutError, OSError):
            return ()
        try:
            facts = parse_order_query(raw, client_order_id=client_order_id)
        except ExecutionParseError:
            return ()
        self._submitted.setdefault(client_order_id, (facts.symbol, facts.exchange_order_id))
        events = self._events_from_query(facts, timestamp=timestamp)
        self._queue.extend(events)
        return events

    # ------------------------------------------------------------------ user stream 桥接（§9 / §10）

    def bridge_user_event(self, observation: OrderUpdateObservation) -> tuple[ExecutionEvent, ...]:
        """把 private runtime 的 `ORDER_TRADE_UPDATE` 事实转成 `ExecutionEvent` 并入队。

        - **成交**：直接产出 `FillReceived`（保留 trade_id/commission/…；绝不由 executedQty 差推造 Fill，§11）；
        - **状态**：映射为 `OrderStatusUpdate`；若与本地状态机冲突（终态不可回退等），**跳过**并计数
          （冲突交由既有 reconciliation 处理，不在 adapter 里造第二套状态机）。
        """
        if not isinstance(observation, OrderUpdateObservation):
            raise ExecutionAdapterError("bridge_user_event requires an OrderUpdateObservation")
        if not is_probex_order(observation.client_order_id):
            return ()
        events: list[ExecutionEvent] = []
        if observation.is_fill and observation.trade_id:
            events.append(
                FillReceived(
                    client_order_id=observation.client_order_id,
                    execution_id=f"binance-trade-{observation.trade_id}",
                    trade_id=str(observation.trade_id),
                    price=observation.last_fill_price,
                    quantity=observation.last_fill_quantity,
                    timestamp=observation.event_ts,
                    fee=observation.commission,
                    fee_asset=observation.commission_asset or "USDT",
                    is_maker=observation.is_maker,
                )
            )
        # 成交类事实**只**产出 FillReceived：既有 `OrderTracker` 会依据成交量推导
        # PARTIALLY_FILLED / FILLED（D-021）。若这里再补一条状态事件，会在"整单成交"时出现
        # 同一批事件内 FILLED -> PARTIALLY_FILLED 的非法回退。
        target = None if observation.is_fill else _STATUS_MAP.get(observation.order_status)
        if target is not None:
            self._submitted.setdefault(
                observation.client_order_id, (observation.symbol, str(observation.order_id))
            )
            if self._can_apply(observation.client_order_id, target):
                events.append(
                    OrderStatusUpdate(
                        client_order_id=observation.client_order_id,
                        status=target,
                        timestamp=observation.event_ts,
                        detail=f"user_stream:{observation.execution_type}",
                        exchange_order_id=str(observation.order_id),
                        filled_quantity=observation.cumulative_fill_quantity,
                        avg_fill_price=observation.average_price or None,
                    )
                )
            else:
                self._skipped_transitions += 1
        self._queue.extend(events)
        return tuple(events)

    # ------------------------------------------------------------------ 本地前置检查

    def _require_submittable(self, order: Order) -> None:
        if order.venue is not self.venue:
            raise SubmitRefusedError("venue_mismatch", order.venue.value)
        if order.symbol != self.symbol:
            raise SubmitRefusedError("symbol_mismatch", order.symbol)
        if not is_probex_order(order.client_order_id):
            raise SubmitRefusedError("ownership_violation", order.client_order_id)
        if not order.post_only:
            raise SubmitRefusedError("post_only_required", "first version only allows GTX/post-only")
        rules = self.rules_provider()
        if rules is None:
            raise SubmitRefusedError("trading_rules_unavailable", "exchangeInfo rules are required before submit")
        self._require_rules(order, rules=rules)
        # §13：adapter **不**判断"是否真的降险"（那是 RiskGate 的业务约束）；reduceOnly 原样透传。
        self._require_authority(order)

    def _require_rules(self, order: Order, *, rules: TradingRules) -> None:
        """§14：不合规就**本地拒绝**，绝不自动 round。"""
        if not rules.is_trading:
            raise SubmitRefusedError("symbol_not_trading", rules.status)
        if not _is_multiple(order.price, rules.tick_size):
            raise SubmitRefusedError("price_not_on_tick", f"{order.price} vs tick {rules.tick_size}")
        if not _is_multiple(order.quantity, rules.step_size):
            raise SubmitRefusedError("quantity_not_on_step", f"{order.quantity} vs step {rules.step_size}")
        if order.quantity < rules.min_qty:
            raise SubmitRefusedError("quantity_below_min", f"{order.quantity} < {rules.min_qty}")
        if order.quantity > rules.max_qty:
            raise SubmitRefusedError("quantity_above_max", f"{order.quantity} > {rules.max_qty}")
        if not rules.min_price <= order.price <= rules.max_price:
            raise SubmitRefusedError("price_out_of_band", f"{order.price} not in [{rules.min_price}, {rules.max_price}]")
        if order.price * order.quantity < rules.min_notional:
            raise SubmitRefusedError("notional_below_min", f"{order.price * order.quantity} < {rules.min_notional}")

    def _require_authority(self, order: Order) -> None:
        """§2 / SC-1 / SC-19：authority 无效 ⇒ **不发送任何 HTTP 请求**。

        P0001.9.7.1：在**最邻近真实写请求**的位置分派 authority kind。
        BOOTSTRAP 分支比 NORMAL 更窄，且在通过校验后**立即消耗**唯一额度（网络调用之前）。
        """
        context = self.authority_provider()
        if not isinstance(context, ExecutionAuthorityContext):
            raise SubmitRefusedError("authority_context_missing")
        authority = context.authority
        if authority is None:
            raise SubmitRefusedError("AUTHORITY_MISSING")
        if isinstance(authority, BootstrapAuthority):
            gate = context.bootstrap_gate
            if gate is None:
                raise SubmitRefusedError(
                    "BOOTSTRAP_WRITE_GATE_MISSING", "bootstrap authority requires an explicit write gate"
                )
            verdict = gate.authorize(
                authority=authority,
                now_ms=self._now_ms(),
                requested_environment=self.environment,
                symbol=order.symbol,
                notional_usdt=float(order.price) * float(order.quantity),
                # adapter 的唯一写路径就是 post-only limit（GTX）；不存在非 post-only 写入口
                post_only=True,
                private_continuity_valid=context.private_continuity_valid,
                latency_status=(
                    PrivateLatencyStatus.UNKNOWN
                    if context.latency_status is None
                    else context.latency_status
                ),
                recovery_generation=context.recovery_generation,
                market_generation=context.market_generation,
                kill_switch_mode=context.kill_switch_mode,
            )
            if not verdict.valid:
                reason = verdict.reasons[0] if verdict.reasons else AuthorityInvalidReason.NOT_LIVE_READY
                raise SubmitRefusedError(reason.value, verdict.details[0] if verdict.details else "")
            return
        verdict = self.validator.validate(
            authority,
            now_ms=self._now_ms(),
            requested_environment=self.environment,
            recovery_generation=context.recovery_generation,
            market_generation=context.market_generation,
            hwm_activation_id=context.hwm_activation_id,
            hwm_generation=context.hwm_generation,
            kill_switch_mode=context.kill_switch_mode,
        )
        if not verdict.valid:
            reason = verdict.reasons[0] if verdict.reasons else AuthorityInvalidReason.NOT_LIVE_READY
            raise SubmitRefusedError(reason.value, verdict.details[0] if verdict.details else "")

    def _require_cancelable(self, order: Order) -> None:
        """§2 / SC-20：撤单是降险动作 —— 只要求所有权/环境/凭据，不要求 LIVE_READY。"""
        if order.venue is not self.venue:
            raise ExecutionAdapterError("cancel refused: venue mismatch")
        if not is_probex_order(order.client_order_id):
            raise ExecutionAdapterError("cancel refused: order does not belong to Probex")
        if order.symbol != self.symbol:
            raise ExecutionAdapterError("cancel refused: symbol mismatch")
        if self.rest.credentials is None:  # pragma: no cover - ApiCredentials 必填
            raise ExecutionAdapterError("cancel refused: credentials unavailable")

    # ------------------------------------------------------------------ 内部

    def _now_ms(self) -> int:
        return int(self.rest.clock())

    def _can_apply(self, client_order_id: str, target: OrderStatus) -> bool:
        if self.local_status_provider is None:
            return True
        current = self.local_status_provider(client_order_id)
        if current is None:
            return True
        if current is target:
            return True
        return can_transition(current, target)

    def _events_from_query(self, facts: OrderQueryFacts, *, timestamp: Milliseconds) -> tuple[ExecutionEvent, ...]:
        target = _STATUS_MAP.get(facts.status)
        if target is None:
            return ()
        if facts.status in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}:
            if target is OrderStatus.FILLED:
                # §11：查询响应**不含** trade_id / commission ⇒ 绝不用它造 canonical Fill。
                # 成交事实只来自 trade execution（user stream / userTrades / reconciliation）。
                return (
                    OrderStatusUpdate(
                        client_order_id=facts.client_order_id,
                        status=OrderStatus.FILLED,
                        timestamp=timestamp,
                        detail="exchange_query:FILLED",
                        exchange_order_id=facts.exchange_order_id,
                        filled_quantity=facts.executed_quantity,
                        avg_fill_price=facts.average_price or None,
                    ),
                )
            if target is OrderStatus.CANCELED:
                return (OrderCanceled(client_order_id=facts.client_order_id, timestamp=timestamp),)
            if target is OrderStatus.EXPIRED:
                return (OrderExpired(client_order_id=facts.client_order_id, timestamp=timestamp),)
            return (
                OrderStatusUpdate(
                    client_order_id=facts.client_order_id,
                    status=OrderStatus.FAILED,
                    timestamp=timestamp,
                    detail="exchange_query:REJECTED",
                    exchange_order_id=facts.exchange_order_id,
                ),
            )
        return (
            OrderStatusUpdate(
                client_order_id=facts.client_order_id,
                status=target,
                timestamp=timestamp,
                detail="exchange_query",
                exchange_order_id=facts.exchange_order_id,
                filled_quantity=facts.executed_quantity,
                avg_fill_price=facts.average_price or None,
            ),
        )


def _is_multiple(value: float, step: float) -> bool:
    """浮点步长校验（不允许 adapter 自动 round，§14）。"""
    if step <= 0.0:
        return False
    quotient = value / step
    return abs(quotient - round(quotient)) <= 1e-6


__all__ = [
    "BinanceExecutionAdapter",
    "ExecutionAdapterError",
    "ExternalFactsProvider",
    "ExternalFactsUnavailableError",
    "ExecutionAuthorityContext",
    "LOCAL_REJECTION_PREFIX",
    "SubmitOutcome",
    "SubmitRefusedError",
]
