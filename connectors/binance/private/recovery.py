"""启动 / 重启 / 断线恢复后的账户与订单收敛编排（P0001.9.3）。

回答的问题是：**「我重新启动以后，现在账户里有什么仓位、挂单、成交，本地 OrderTracker / Accounting 是否可信？」**

```text
PrivateAccountRuntime（stream 必须先 ACTIVE、continuity_assumed、boundary 有效）
        ↓
只读 facts：account / positionRisk / openOrders / allOrders / userTrades
        ↓
RecoverySnapshot（归一化为既有 ExternalOrder / ExternalFill；只保留 probex- 订单）
        ↓
OrderTracker ⇄ reconcile()（ADOPT / RESTORE / MARKED_LOST / 补 canonical Fill / unresolved）
        ↓
一次性 AccountingCore startup baseline（**不产生 synthetic Fill**）
        ↓
RecoveryGate → RECOVERED | BLOCKED(+reason codes)
```

纪律：

- **任何未知即 BLOCKED**（fail closed），绝不"当作没问题"；
- 非 Probex 的**未平挂单** ⇒ `BLOCKED: FOREIGN_OPEN_ORDER`（不忽略、不自动撤、不 adopt）；
- 历史 PnL 不伪造：baseline 之后相关指标为 UNKNOWN，`RiskGate` 继续 fail closed（见 D-037）；
- stream 断开 ⇒ recovery 状态立即回落为 `NOT_RECOVERED`，**即使自动重连成功也不得自动 RECOVERED**。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from execution.reconciliation import ReconciliationActionKind, ReconciliationReport, reconcile
from execution.tracker import OrderTracker
from market.events.types import Milliseconds, Venue
from portfolio.accounting import AccountingCore, BaselineAlreadyAppliedError, BaselineApplication
from portfolio.types import ExternalAccountBaseline
from execution.types import ExternalFill, ExternalOrder

from connectors.binance.market_data.errors import MarketDataFormatError, TransportError
from connectors.binance.private.account import AccountSnapshotObservation, parse_account_snapshot
from connectors.binance.private.errors import (
    PrivateApiError,
    PrivateFormatError,
    UnsupportedAccountModeError,
)
from connectors.binance.private.orders import (
    classify_orders,
    latest_by_client_order_id,
    parse_external_orders,
    sort_orders_by_time,
)
from connectors.binance.private.positions import (
    ONE_WAY_POSITION_SIDE,
    SUPPORTED_MARGIN_ASSET,
    PositionObservation,
    parse_position_risk,
)
from connectors.binance.private.rest import PrivateRestClient
from connectors.binance.private.trades import parse_external_fills

#: 允许 bootstrap 的 baseline 来源。
BASELINE_SOURCE = "BINANCE_RECOVERY"

#: 恢复**不能**接受的 reconcile 动作：事实自相矛盾或无法 adopt ⇒ 必须 BLOCKED。
#: 其余动作（ADOPTED / RESTORED / MARKED_LOST / QUANTITY_CORRECTED / STATUS_CORRECTED）
#: 是「已被正确解决的差异」，不是失败（`MARKED_LOST` 的未确定暴露由 OrderTracker 以
#: uncertain exposure 表达，见 D-021/D-022；这里额外作为 telemetry 上报）。
UNRESOLVABLE_ACTION_KINDS = frozenset(
    {ReconciliationActionKind.STATUS_CONFLICT, ReconciliationActionKind.ADOPT_REJECTED}
)

#: 默认历史/成交拉取条数（只读；由调用方显式覆盖，无业务含义）。
DEFAULT_FACT_LIMIT = 100


class RecoveryStatus(Enum):
    """恢复状态。"""

    #: 初始态 / stream 断开后回落态：必须先跑完整恢复流程。
    NOT_RECOVERED = "not_recovered"
    RECOVERED = "recovered"
    BLOCKED = "blocked"


class RecoveryReason(Enum):
    """`BLOCKED` 的原因码（任何一项未知/失败都会出现在这里）。"""

    STREAM_NOT_ACTIVE = "STREAM_NOT_ACTIVE"
    CONTINUITY_NOT_ASSUMED = "CONTINUITY_NOT_ASSUMED"
    BOUNDARY_MISSING = "BOUNDARY_MISSING"
    ACCOUNT_SNAPSHOT_FAILED = "ACCOUNT_SNAPSHOT_FAILED"
    POSITION_SNAPSHOT_FAILED = "POSITION_SNAPSHOT_FAILED"
    OPEN_ORDERS_READ_FAILED = "OPEN_ORDERS_READ_FAILED"
    ORDER_HISTORY_READ_FAILED = "ORDER_HISTORY_READ_FAILED"
    FILLS_READ_FAILED = "FILLS_READ_FAILED"
    FOREIGN_OPEN_ORDER = "FOREIGN_OPEN_ORDER"
    UNRESOLVED_ORDERS = "UNRESOLVED_ORDERS"
    RECONCILIATION_NOT_CONVERGED = "RECONCILIATION_NOT_CONVERGED"
    BASELINE_MISMATCH = "BASELINE_MISMATCH"
    BASELINE_ALREADY_APPLIED = "BASELINE_ALREADY_APPLIED"
    ACCOUNT_MODE_UNSUPPORTED = "ACCOUNT_MODE_UNSUPPORTED"
    SYMBOL_CONTRACT_VIOLATION = "SYMBOL_CONTRACT_VIOLATION"


class RecoveryReadError(PrivateApiError):
    """某一步只读事实读取/解析失败；携带唯一 reason code（不靠字符串匹配）。"""

    def __init__(self, reason: RecoveryReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StreamState:
    """恢复门需要的 private stream 事实（由 P0001.9.2 runtime 提供）。"""

    listen_key_state: str
    continuity_assumed: bool
    boundary_present: bool

    @property
    def active(self) -> bool:
        return self.listen_key_state == "ACTIVE"


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """一次性读取到的外部事实（只读、只含 Probex 自己的订单）。"""

    symbol: str
    captured_at: Milliseconds
    account: AccountSnapshotObservation
    position: PositionObservation
    open_orders: tuple[ExternalOrder, ...]
    history: tuple[ExternalOrder, ...]
    fills: tuple[ExternalFill, ...]
    foreign_open_orders: tuple[ExternalOrder, ...]
    foreign_ignored: int
    unresolved_fill_orders: int

    def baseline(self) -> ExternalAccountBaseline:
        """由账户 + 持仓快照构造 startup baseline（不使用任何 synthetic Fill）。"""
        return ExternalAccountBaseline(
            symbol=self.symbol,
            wallet_balance=self.account.total_wallet_balance,
            available_balance=self.account.available_balance,
            position_qty=self.position.position_amt,
            entry_price=self.position.entry_price,
            mark_price=self.position.mark_price,
            liquidation_price=self.position.liquidation_price,
            captured_at=self.captured_at,
            source=BASELINE_SOURCE,
        )


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """一次恢复尝试的结果。"""

    status: RecoveryStatus
    reasons: tuple[RecoveryReason, ...]
    detail: str
    snapshot: RecoverySnapshot | None = None
    report: ReconciliationReport | None = None
    baseline: BaselineApplication | None = None
    #: 本次恢复中被判定为 LOST（外部无记录）的本地订单：暴露不确定但已被显式建模。
    uncertain_orders: tuple[str, ...] = ()

    @property
    def recovered(self) -> bool:
        return self.status is RecoveryStatus.RECOVERED


@dataclass
class StartupRecovery:
    """把「交易所事实 → OrderTracker + Accounting」的启动恢复编排成一次可重复、可审计的动作。"""

    rest: PrivateRestClient
    tracker: OrderTracker
    accounting: AccountingCore
    symbol: str
    venue: Venue = Venue.BINANCE
    #: 时间来源必须由调用方注入（不使用 wall-clock，也不设默认值）。
    clock: Callable[[], Milliseconds] = field(default=None)  # type: ignore[assignment]
    fact_limit: int = DEFAULT_FACT_LIMIT
    state: RecoveryStatus = RecoveryStatus.NOT_RECOVERED
    last_result: RecoveryResult | None = None
    _reason_log: list[tuple[str, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise PrivateFormatError("StartupRecovery.symbol must be a non-empty string")
        if not callable(self.clock):
            raise PrivateFormatError("StartupRecovery.clock must be injected (no default time source)")
        if isinstance(self.fact_limit, bool) or not isinstance(self.fact_limit, int) or self.fact_limit < 1:
            raise PrivateFormatError("StartupRecovery.fact_limit must be an int >= 1")

    # ------------------------------------------------------------------ 状态

    def invalidate(self, *, reason: str = "stream disconnected") -> None:
        """stream 断开 ⇒ 恢复状态立即失效（即使随后自动重连成功也不得自动 RECOVERED）。"""
        self.state = RecoveryStatus.NOT_RECOVERED
        self._reason_log.append((reason, int(self.clock())))

    @property
    def reason_log(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._reason_log)

    # ------------------------------------------------------------------ 事实读取

    def fetch_snapshot(self) -> RecoverySnapshot:
        """读取全部只读事实并归一化（读取失败会抛出，由 `run()` 归类为 BLOCKED）。

        可直接作为 `run(snapshot_provider=...)` 使用。
        """
        captured_at = int(self.clock())
        account = self._read(
            RecoveryReason.ACCOUNT_SNAPSHOT_FAILED,
            lambda: parse_account_snapshot(
                self.rest.account_snapshot(), symbol=self.symbol, receive_ts=captured_at, process_ts=captured_at
            ),
        )
        position = self._read(
            RecoveryReason.POSITION_SNAPSHOT_FAILED,
            lambda: parse_position_risk(
                self.rest.position_risk(), symbol=self.symbol, receive_ts=captured_at, process_ts=captured_at
            ),
        )
        open_orders = self._read(
            RecoveryReason.OPEN_ORDERS_READ_FAILED,
            lambda: parse_external_orders(self.rest.open_orders(self.symbol), symbol=self.symbol, venue=self.venue),
        )
        history = self._read(
            RecoveryReason.ORDER_HISTORY_READ_FAILED,
            lambda: parse_external_orders(
                sort_orders_by_time(self.rest.order_history(self.symbol, limit=self.fact_limit)),
                symbol=self.symbol,
                venue=self.venue,
            ),
        )
        latest_history = tuple(latest_by_client_order_id(history).values())
        facts = classify_orders(open_orders=open_orders, history=latest_history)

        order_id_to_client: dict[int, str] = {}
        for order in open_orders + history:
            if order.exchange_order_id is not None:
                order_id_to_client[int(order.exchange_order_id)] = order.client_order_id
        fill_facts = self._read(
            RecoveryReason.FILLS_READ_FAILED,
            lambda: parse_external_fills(
                self.rest.user_trades(self.symbol, limit=self.fact_limit),
                symbol=self.symbol,
                order_id_to_client_id=order_id_to_client,
                venue=self.venue,
                fee_asset=self.accounting.settlement_asset,
            ),
        )

        return RecoverySnapshot(
            symbol=self.symbol,
            captured_at=captured_at,
            account=account,
            position=position,
            open_orders=facts.open_orders,
            history=facts.history_orders,
            fills=fill_facts.fills,
            foreign_open_orders=facts.foreign_open_orders,
            foreign_ignored=facts.foreign_ignored,
            unresolved_fill_orders=fill_facts.unresolved_order_ids,
        )

    # ------------------------------------------------------------------ 编排

    def run(
        self,
        *,
        stream_state: StreamState,
        snapshot: RecoverySnapshot | None = None,
        snapshot_provider: Callable[[], RecoverySnapshot] | None = None,
    ) -> RecoveryResult:
        """执行一次完整恢复：门控 → facts → reconcile → baseline → RecoveryGate。"""
        reasons = self._stream_reasons(stream_state)
        if reasons:
            return self._blocked(reasons, "private stream is not in a recoverable state")

        if snapshot is None:
            if snapshot_provider is None:
                raise PrivateFormatError("run() requires either snapshot or snapshot_provider")
            try:
                snapshot = snapshot_provider()
            except UnsupportedAccountModeError as exc:
                return self._blocked(
                    (RecoveryReason.ACCOUNT_MODE_UNSUPPORTED,), f"unsupported account mode: {exc}"
                )
            except RecoveryReadError as exc:
                return self._blocked((exc.reason,), str(exc))
            except self.READ_ERRORS as exc:  # type: ignore[misc]
                return self._blocked(
                    (self._read_reason(exc),), f"fact read failed: {self._read_reason(exc).value}"
                )

        contract_violation = self._verify_snapshot(snapshot)
        if contract_violation is not None:
            reason, detail = contract_violation
            return self._blocked((reason,), detail, snapshot=snapshot)

        if snapshot.foreign_open_orders:
            return self._blocked(
                (RecoveryReason.FOREIGN_OPEN_ORDER,),
                f"{len(snapshot.foreign_open_orders)} non-probex open order(s) present; "
                "this system must not assume it owns the full account state",
                snapshot=snapshot,
            )

        report = reconcile(
            self.tracker,
            external_open_orders=snapshot.open_orders,
            external_recent_fills=snapshot.fills,
            external_history=snapshot.history,
            timestamp=snapshot.captured_at,
            venue=self.venue,
        )

        unresolved = self.tracker.unresolved_orders()
        if unresolved:
            return self._blocked(
                (RecoveryReason.UNRESOLVED_ORDERS,),
                f"{len(unresolved)} order(s) lack sufficient data; unknown exposure must not be treated as zero",
                snapshot=snapshot,
                report=report,
            )
        unresolvable = tuple(action for action in report.actions if action.kind in UNRESOLVABLE_ACTION_KINDS)
        if unresolvable:
            return self._blocked(
                (RecoveryReason.RECONCILIATION_NOT_CONVERGED,),
                f"reconciliation produced unresolvable actions: "
                f"{[action.kind.value for action in unresolvable]}",
                snapshot=snapshot,
                report=report,
            )

        baseline_result, baseline_reason, baseline_detail = self._apply_baseline(snapshot)
        if baseline_reason is not None:
            return self._blocked((baseline_reason,), baseline_detail, snapshot=snapshot, report=report)
        if baseline_result is None:
            # 已有 baseline（例如第二次启动）：只补 baseline 水位线**之后**的成交。
            # captured_at 之前的成交已经包含在 baseline 里，重复入账会凭空改变余额/仓位。
            watermark = self.accounting.baseline.captured_at if self.accounting.baseline else 0
            for fill in report.canonical_fills:
                if fill.exchange_ts > watermark:
                    self.accounting.record_fill(fill)

        mismatch = self._position_mismatch(snapshot)
        if mismatch is not None:
            return self._blocked((RecoveryReason.BASELINE_MISMATCH,), mismatch, snapshot=snapshot, report=report)

        result = RecoveryResult(
            status=RecoveryStatus.RECOVERED,
            reasons=(),
            detail="account and order state reconciled; accounting baseline applied once",
            snapshot=snapshot,
            report=report,
            baseline=baseline_result if isinstance(baseline_result, BaselineApplication) else None,
            uncertain_orders=tuple(
                action.client_order_id
                for action in report.actions
                if action.kind is ReconciliationActionKind.MARKED_LOST
            ),
        )
        self.state = RecoveryStatus.RECOVERED
        self.last_result = result
        return result

    # ------------------------------------------------------------------ 内部

    def _apply_baseline(
        self, snapshot: RecoverySnapshot
    ) -> tuple[BaselineApplication | None, RecoveryReason | None, str]:
        """一次性 bootstrap：只在「尚未 bootstrap 且尚无本 session Fill」时允许（否则由 AccountingCore 拒绝）。"""
        if self.accounting.baseline_applied:
            existing = self.accounting.baseline
            if existing is not None and existing.source != BASELINE_SOURCE:
                return None, RecoveryReason.BASELINE_MISMATCH, "baseline source mismatch"
            return None, None, "already bootstrapped; accounting state is authoritative"
        try:
            applied = self.accounting.bootstrap_from_baseline(snapshot.baseline())
        except BaselineAlreadyAppliedError as exc:
            return None, RecoveryReason.BASELINE_ALREADY_APPLIED, str(exc)
        if abs(applied.balance - snapshot.account.total_wallet_balance) > 1e-9:
            return None, RecoveryReason.BASELINE_MISMATCH, "balance mismatch right after bootstrap"
        return applied, None, "baseline applied once"

    def _position_mismatch(self, snapshot: RecoverySnapshot) -> str | None:
        """用**新鲜的** positionRisk 复核 accounting 持仓（baseline 与外部状态必须一致）。

        复核读取失败 ⇒ 返回失败说明（调用方按 BASELINE_MISMATCH 处理，fail closed）。
        """
        try:
            fresh = self._read(
                RecoveryReason.POSITION_SNAPSHOT_FAILED,
                lambda: parse_position_risk(
                    self.rest.position_risk(),
                    symbol=self.symbol,
                    receive_ts=int(self.clock()),
                    process_ts=int(self.clock()),
                ),
            )
        except self.READ_ERRORS as exc:
            return f"post-recovery position re-read failed: {type(exc).__name__}"
        local = self.accounting.position(self.symbol)
        if abs(local.qty - fresh.position_amt) > 1e-9:
            return (
                f"accounting position {local.qty} does not match exchange position {fresh.position_amt} "
                "(baseline/stream race? fail closed)"
            )
        if abs(fresh.mark_price - (self.accounting.mark_price(self.symbol) or 0.0)) > 1e-6:
            self.accounting.update_mark_price(self.symbol, fresh.mark_price, timestamp=int(self.clock()))
        return None

    #: 只读事实读取过程可能出现的边界错误（私有报文 + 传输层）。
    READ_ERRORS = (PrivateApiError, MarketDataFormatError, TransportError)

    def _read(self, reason: RecoveryReason, reader: Callable[[], object]) -> object:
        """执行一步只读读取，失败时抛出携带 reason code 的 `RecoveryReadError`。"""
        try:
            return reader()
        except UnsupportedAccountModeError:
            raise
        except self.READ_ERRORS as exc:
            raise RecoveryReadError(reason, type(exc).__name__) from exc

    def _verify_snapshot(self, snapshot: RecoverySnapshot) -> tuple[RecoveryReason, str] | None:
        """防御性校验：one-way / USDT-M / symbol 契约（解析层已校验，这里对注入的 snapshot 同样成立）。"""
        symbol = self.symbol
        position = snapshot.position
        if position.position_side != ONE_WAY_POSITION_SIDE:
            return (
                RecoveryReason.ACCOUNT_MODE_UNSUPPORTED,
                f"positionSide={position.position_side!r} is not one-way mode",
            )
        if position.margin_asset is not None and position.margin_asset != SUPPORTED_MARGIN_ASSET:
            return (
                RecoveryReason.ACCOUNT_MODE_UNSUPPORTED,
                f"margin asset {position.margin_asset!r} is not {SUPPORTED_MARGIN_ASSET}-M",
            )
        if snapshot.account.symbol != symbol or position.symbol != symbol:
            return (RecoveryReason.SYMBOL_CONTRACT_VIOLATION, "account/position symbol mismatch")
        if symbol not in {entry[0] for entry in snapshot.account.position_sides}:
            return (
                RecoveryReason.SYMBOL_CONTRACT_VIOLATION,
                f"account snapshot has no position entry for {symbol}",
            )
        for order in snapshot.open_orders + snapshot.history:
            if order.symbol != symbol:
                return (RecoveryReason.SYMBOL_CONTRACT_VIOLATION, f"order symbol {order.symbol!r} mismatch")
        # `ExternalFill`（既有契约）不含 symbol；symbol 契约在归一化时已按 symbol 过滤（trades.py）
        return None

    def _stream_reasons(self, stream_state: StreamState) -> tuple[RecoveryReason, ...]:
        reasons: list[RecoveryReason] = []
        if not stream_state.active:
            reasons.append(RecoveryReason.STREAM_NOT_ACTIVE)
        if not stream_state.continuity_assumed:
            reasons.append(RecoveryReason.CONTINUITY_NOT_ASSUMED)
        if not stream_state.boundary_present:
            reasons.append(RecoveryReason.BOUNDARY_MISSING)
        return tuple(reasons)

    def _read_reason(self, error: Exception) -> RecoveryReason:
        """兜底分类（正常情况下读取失败已经被包装成 `RecoveryReadError`）。"""
        if isinstance(error, RecoveryReadError):
            return error.reason
        if isinstance(error, UnsupportedAccountModeError):
            return RecoveryReason.ACCOUNT_MODE_UNSUPPORTED
        return RecoveryReason.ACCOUNT_SNAPSHOT_FAILED

    def _blocked(
        self,
        reasons: tuple[RecoveryReason, ...],
        detail: str,
        *,
        snapshot: RecoverySnapshot | None = None,
        report: ReconciliationReport | None = None,
    ) -> RecoveryResult:
        result = RecoveryResult(
            status=RecoveryStatus.BLOCKED,
            reasons=reasons,
            detail=detail,
            snapshot=snapshot,
            report=report,
        )
        self.state = RecoveryStatus.BLOCKED
        self.last_result = result
        return result


__all__ = [
    "BASELINE_SOURCE",
    "DEFAULT_FACT_LIMIT",
    "RecoveryReadError",
    "RecoveryReason",
    "RecoveryResult",
    "RecoverySnapshot",
    "RecoveryStatus",
    "StartupRecovery",
    "UNRESOLVABLE_ACTION_KINDS",
    "StreamState",
]
