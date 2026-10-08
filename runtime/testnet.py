"""TESTNET execution composition（P0001.16 §2–§13）：把既有 Binance 私有能力接入统一 connector seam。

**唯一写路径**（§2/§17）：

```
ExecutionEngine → PrivateExecutionConnector → BinancePrivateExecutionConnector → Binance TESTNET
```

- `ExecutionEngine` 只依赖 `PrivateExecutionConnector`（P0001.15 契约）；Binance-specific 映射留在 connector 内。
- `OrderTracker` 仍是**唯一**订单 Owner；`AccountingCore` 仍是**唯一**账本 Owner；`FillLedger` 只做 canonical Fill 的
  追加记录（不是第二 Owner）。
- 真实 user stream 是订单/成交的**主事实源**（§9）；REST 只用于 startup recovery / reconciliation / UNKNOWN 收敛。
- submit 前必须 `Risk allow → LiveReadinessAuthority → authority valid`（§5）；authority 由 `readiness` 既有契约签发，
  本模块**不复制**任何 readiness 规则。
- TESTNET acceptance capability（§14 B + 人类裁决）默认不存在：只有显式许可被传进来时，adapter 才接受非 post-only 单，
  且只走 **IOC**（永不挂单）。产品默认策略路径仍是 `LIMIT + GTX + post-only`。
"""

from __future__ import annotations

import json as _json
import time
from builtins import ValueError as _ValueError  # noqa: F401 - 保持导入稳定性
from dataclasses import dataclass, field
from typing import Callable

from connectors.binance.execution.adapter import (BinanceExecutionAdapter, ExecutionAuthorityContext,
                                                  normalizer_from_rules)
from connectors.binance.execution.rest import BinanceExecutionRestClient
from connectors.binance.market_data.runtime import LiveMarketDataConfig, LiveMarketDataRuntime
from connectors.binance.market_data.snapshot import UrllibJsonClient
from connectors.binance.market_data.transport import ReconnectPolicy, connect
from connectors.binance.private.auth import ApiCredentials, ServerTimeOffset
from connectors.binance.private.income import MAX_PAGE_LIMIT, fetch_income_history
from connectors.binance.private.recovery import StartupRecovery, StreamState
from connectors.binance.private.rest import PrivateRestClient, UrllibRestFetcher
from connectors.binance.private.runtime import PrivateAccountConfig, PrivateAccountRuntime
from connectors.binance.private.user_stream import UserStreamClient
from connectors.binance.execution_connector import BinancePrivateExecutionConnector
from execution.acceptance import TestnetAcceptancePermission
from execution.engine import ExecutionEngine, ExecutionResult
from execution.manager import OrderManager
from execution.normalization import OrderNormalizer
from execution.tracker import OrderTracker
from execution.types import Order, OrderStatus
from market.events.types import Milliseconds, Venue
from market.readiness import MarketReadinessEvidence, MarketReadinessPolicy, build_market_evidence
from portfolio.accounting import AccountingCore
from portfolio.fills import FillLedger
from readiness import (Environment, LiveReadinessGate, LiveReadinessResult, LiveReadinessStatus,
                       LiveRiskPolicy, PrivateLatencyStatus, ReadinessPolicy, issue_authority)
from readiness.collector import ReadinessEvidenceCollector
from readiness.evidence import (environment_evidence, exchange_available_balance,
                                historical_risk_baseline_from_income)
from risk.budget import remaining_exposure_budget
from risk.gate import RiskGate
from risk.high_watermark import HighWatermarkTracker
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import KillSwitchMode, OrderProposal
from readiness.bootstrap import (BootstrapActivation, BootstrapAuthority, BootstrapAuthorityCoordinator,
                                  BootstrapEligibility, BootstrapWriteGate)
from storage.high_watermark import JsonHighWatermarkStore


@dataclass(frozen=True, slots=True)
class LifecycleFact:
    """F-08 订单生命周期事实（字段搬运自 OrderTracker；不新建 event store）。"""

    client_order_id: str
    timestamp: Milliseconds
    event_name: str
    reason: str = ""

    @property
    def ts(self) -> Milliseconds:
        return self.timestamp


@dataclass(frozen=True, slots=True)
class FillFact:
    """F-08 成交事实（`Fill.order_id` 即 client_order_id）。"""

    client_order_id: str
    ts: Milliseconds
    price: float
    quantity: float
    fee: float
    trade_id: str


@dataclass(frozen=True, slots=True)
class ReconciliationFact:
    """UNKNOWN 收敛 / reconciliation 证据（P0001.16 §10；产品/助手可见）。"""

    ts: Milliseconds
    identity: str
    identity_kind: str
    outcome: str
    reason_code: str
    detail: str


class _NullLatency:
    """未接线时的空延迟观测（`samples()` 返回空 ⇒ 产品层如实 ABSENT）。"""

    def samples(self, *, stage: str | None = None) -> tuple[object, ...]:
        return ()


class TestnetError(RuntimeError):
    """TESTNET composition 契约/状态错误。"""


@dataclass(frozen=True, slots=True)
class TestnetConfig:
    """TESTNET 组合的显式配置（不设业务默认值）。"""

    symbol: str
    #: 真实资金阶段必须显式配置的 risk policy（唯一数值来源；`RiskGate` 的 limits 由它派生）
    risk_policy: LiveRiskPolicy
    #: readiness 判定阈值（全部必填，由 operator 显式给出；本模块不提供默认值）
    readiness_policy: ReadinessPolicy
    state_dir: str
    authority_ttl_ms: int
    environment: Environment = Environment.TESTNET
    #: TESTNET 端点（D-034 边界）：默认即 TESTNET host，可显式覆盖，绝不指向 MAINNET
    rest_base: str = "https://demo-fapi.binance.com"
    ws_host: str = "wss://fstream.binancefuture.com"
    acceptance: TestnetAcceptancePermission | None = None
    #: public 行情观察窗口（market evidence 需要真实窗口覆盖，不是"立刻 known"）
    market_window_s: float = 45.0
    #: 归一化舍入模式（业务选择，必须显式给出；本模块不提供默认值）
    price_rounding: str = ""
    quantity_rounding: str = ""
    market_max_feed_age_ms: int = 5_000
    market_max_mark_age_ms: int = 5_000
    income_max_pages: int = 20

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise TestnetError("TestnetConfig.symbol must be a non-empty string")
        if not isinstance(self.risk_policy, LiveRiskPolicy):
            raise TestnetError("TestnetConfig.risk_policy must be LiveRiskPolicy (explicit operator values)")
        if not isinstance(self.readiness_policy, ReadinessPolicy):
            raise TestnetError(
                "TestnetConfig.readiness_policy must be ReadinessPolicy (explicit operator thresholds)")
        if not isinstance(self.state_dir, str) or not self.state_dir:
            raise TestnetError("TestnetConfig.state_dir must be a non-empty path")
        if isinstance(self.authority_ttl_ms, bool) or not isinstance(self.authority_ttl_ms, int) \
                or self.authority_ttl_ms <= 0:
            raise TestnetError("TestnetConfig.authority_ttl_ms must be a positive int (explicit)")
        for name in ("price_rounding", "quantity_rounding"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise TestnetError(
                    f"TestnetConfig.{name} must be an explicit rounding mode "
                    "(normalization is required before any real write)")
        if self.environment is not Environment.TESTNET:
            raise TestnetError(f"this composition is TESTNET only, got {self.environment.value}")
        if self.acceptance is not None and not isinstance(self.acceptance, TestnetAcceptancePermission):
            raise TestnetError("TestnetConfig.acceptance must be a TestnetAcceptancePermission or None")
        for name in ("rest_base", "ws_host"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise TestnetError(f"TestnetConfig.{name} must be a non-empty string")
        if "testnet" not in self.rest_base and "demo-fapi" not in self.rest_base:
            raise TestnetError(f"TestnetConfig.rest_base must be a TESTNET endpoint, got {self.rest_base!r}")
        if "testnet" not in self.ws_host and "binancefuture" not in self.ws_host:
            raise TestnetError(f"TestnetConfig.ws_host must be a TESTNET endpoint, got {self.ws_host!r}")


@dataclass
class TestnetStack:
    """TESTNET 运行时组合（唯一写路径 + 真实 user stream + readiness authority）。"""

    config: TestnetConfig
    market: LiveMarketDataRuntime
    private: PrivateAccountRuntime
    private_rest: PrivateRestClient
    adapter: BinanceExecutionAdapter
    connector: BinancePrivateExecutionConnector
    tracker: OrderTracker
    manager: OrderManager
    engine: ExecutionEngine
    accounting: AccountingCore
    ledger: FillLedger
    recovery: StartupRecovery
    high_watermark: HighWatermarkTracker
    clock: Callable[[], Milliseconds]
    gate: LiveReadinessGate
    _readiness: LiveReadinessResult | None = field(default=None, init=False)
    _authority: object | None = field(default=None, init=False)
    _public_started: bool = field(default=False, init=False)
    _private_started: bool = field(default=False, init=False)
    _market_evidence: MarketReadinessEvidence | None = field(default=None, init=False)
    _fills_recorded: int = field(default=0, init=False)
    _coordinator: BootstrapAuthorityCoordinator | None = field(default=None, init=False)
    _collected_evidence: object | None = field(default=None, init=False)
    _market_baseline: dict[str, int] | None = field(default=None, init=False)
    _reconciliation_evidence: list[dict[str, object]] = field(default_factory=list, init=False)
    #: 产品读模型事实（字段搬运；不新建第二套 store）
    _transitions: list[LifecycleFact] = field(default_factory=list, init=False)
    _reconciliation_facts: list[ReconciliationFact] = field(default_factory=list, init=False)
    _latency: object | None = field(default=None, init=False)
    _runtime_id: str = field(default="", init=False)
    _started_at: Milliseconds = field(default=0, init=False)
    _server: object | None = field(default=None, init=False)
    #: adapter 的 authority 提供者（由本 composition 在签发后填充；未签发 ⇒ submit 必被拒）
    authority_slot: dict[str, ExecutionAuthorityContext | None] = field(default_factory=lambda: {"context": None})

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """连接 public + private，执行 startup recovery（不产生任何写请求）。"""
        self.market.load_trading_rules()
        self.market.measure_server_time_offset()
        self.market.connect()
        self._public_started = True
        self.private.start()
        self._private_started = True
        self.private.refresh_clock_calibration()
        self.recovery.run(stream_state=self._stream_state(), snapshot_provider=self.recovery.fetch_snapshot)

    def close(self) -> None:
        if self._private_started:
            self.private.stop()
            self._private_started = False
        if self._public_started:
            self.market.close()
            self._public_started = False

    # ------------------------------------------------------------------ 事实泵

    def pump_market(self, *, seconds: float) -> int:
        """pump public 行情；同时把**正式 mark** 注入 accounting（P0001.15 的 MARK 语义）。"""
        deadline = time.time() + max(0.0, seconds)
        events = 0
        while time.time() < deadline:
            batch = self.market.pump_once(timeout_s=0.5, max_messages=64)
            events += len(batch.market_events)
            if batch.mark is not None:
                self.accounting.update_mark_price(self.config.symbol, float(batch.mark.price),
                                                  timestamp=int(batch.mark.receive_ts))
        return events

    def start_market_window(self, *, timeout_s: float = 20.0) -> bool:
        """开始一个真实 market 观察窗口：等到盘口 HEALTHY 后记录计数器基线（避免把启动对齐算成 gap）。"""
        healthy = self.wait_for_book_healthy(timeout_s=timeout_s)
        self._market_baseline = self.market_baseline()
        return healthy

    def wait_for_book_healthy(self, *, timeout_s: float = 20.0) -> bool:
        """等到盘口首次 HEALTHY（初始快照/锚定完成）——之后才开始计窗口，避免把启动对齐误报成连续性问题。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.market.pump_once(timeout_s=0.5, max_messages=64)
            if str(self.market.engine.book_health.value).lower() == "healthy":
                return True
        return False

    def market_baseline(self) -> dict[str, int]:
        """窗口起点计数器（真实窗口增量判定用；不修改任何判定规则）。"""
        telemetry = self.market.telemetry
        return {"depth_gap_count": int(telemetry.depth_gap_count), "resync_count": int(telemetry.resync_count),
                "malformed_message_count": int(telemetry.malformed_message_count),
                "agg_trade_count": int(telemetry.agg_trade_count)}

    def collect_market_evidence(self, *, baseline: dict[str, int] | None = None) -> MarketReadinessEvidence:
        """由真实 public 事实构造 market evidence（只读取，判定交给 `market.readiness` 既有规则）。"""
        telemetry = self.market.telemetry
        history = self.market.history
        now_ms = int(self.clock())
        feed_age_ms = None if not history else max(0, now_ms - history[-1].time.as_of_receive_ts)
        base = baseline if baseline is not None else (self._market_baseline or {})
        evidence = build_market_evidence(
            observed_at=now_ms, generation=int(self.market.market_generation),
            book_health=self.market.engine.book_health.value, anchored=bool(history),
            mark_age_ms=telemetry.mark_age_ms, feed_age_ms=feed_age_ms,
            depth_gap_count=int(telemetry.depth_gap_count) - int(base.get("depth_gap_count", telemetry.depth_gap_count)),
            resync_count=int(telemetry.resync_count) - int(base.get("resync_count", telemetry.resync_count)),
            malformed_count=int(telemetry.malformed_message_count)
            - int(base.get("malformed_message_count", telemetry.malformed_message_count)),
            agg_trade_count=int(telemetry.agg_trade_count) - int(base.get("agg_trade_count", 0)),
            policy=MarketReadinessPolicy(max_mark_age_ms=self.config.market_max_mark_age_ms,
                                         max_feed_age_ms=self.config.market_max_feed_age_ms))
        self._market_evidence = evidence
        return evidence

    def pump_private(self, *, seconds: float) -> int:
        """pump private user stream：stream → connector bridge → engine.poll（fill 由 stream 驱动，§7/§9）。"""
        deadline = time.time() + max(0.0, seconds)
        delivered = 0
        while True:
            batch = self.private.pump_once(timeout_s=0.2, max_messages=64)
            for event in getattr(batch, "events", ()) or ():
                # 只有 order 事实能进 bridge（account 事实由 Runtime 的快照/telemetry 提供）
                if hasattr(event, "order_status") or type(event).__name__ == "OrderUpdateObservation":
                    self.connector.bridge_user_event(event)
            result = self.engine.poll(now_ms=int(self.clock()))
            self._note_updates(result.updates)
            delivered += len(result.updates)
            for update in result.updates:
                fill = getattr(update, "fill", None)
                if fill is not None and self.ledger.record(fill).value == "RECORDED":
                    self._fills_recorded += 1
            if time.time() >= deadline:
                return delivered

    # ------------------------------------------------------------------ readiness / authority

    def collect_readiness(self) -> LiveReadinessResult:
        """用真实事实评估 readiness（生产 `ReadinessEvidenceCollector` + 既有 `LiveReadinessGate`）。"""
        now_ms = int(self.clock())
        day_start = utc_day_start_ms(now_ms)
        observation = self.private.latest_snapshot
        if observation is None:
            raise TestnetError("no account snapshot available; refresh_snapshot first")
        income = fetch_income_history(self.private_rest, window_start_ms=day_start, cutoff_ms=now_ms,
                                      page_limit=MAX_PAGE_LIMIT, max_pages=self.config.income_max_pages)
        baseline = historical_risk_baseline_from_income(income, day_start_ts=day_start, cutoff_ts=now_ms)
        collector = ReadinessEvidenceCollector(
            runtime=self.private, recovery=self.recovery,
            market=self._market_evidence or self.collect_market_evidence(),
            environment=environment_evidence(environment=self.config.environment),
            risk_policy=self.config.risk_policy, high_watermark=self.high_watermark)
        collected = collector.collect(accounting=self.accounting, now_ms=now_ms, day_start_ts=day_start,
                                      historical_baseline=baseline,
                                      exchange_available_balance=exchange_available_balance(observation))
        result = self.gate.evaluate(collected.evidence)
        self._readiness = result
        self._collected_evidence = collected.evidence
        self._provenance = collected.provenance  # type: ignore[attr-defined]
        return result

    @property
    def last_collected_evidence(self) -> object | None:
        """最近一次 collect 的 readiness evidence（只读；供上层判断既有事实，如 daily_pnl_known）。"""
        return self._collected_evidence

    def issue_authority(self) -> object:
        """由 LIVE_READY 结果签发 authority；非 LIVE_READY ⇒ 抛错（绝不产生"半授权"）。"""
        result = self._readiness if self._readiness is not None else self.collect_readiness()
        if result.status is not LiveReadinessStatus.LIVE_READY:
            reasons = ",".join(reason.value for reason in (getattr(result, "reasons", ()) or ()))
            raise TestnetError(f"readiness is {result.status.value}; refusing to issue authority ({reasons})")
        provenance = getattr(self, "_provenance", None)
        if provenance is None:
            raise TestnetError("readiness provenance is missing; collect_readiness() must run first")
        authority = issue_authority(result, provenance=provenance,
                                    authority_id=f"testnet-{int(self.clock())}", now_ms=int(self.clock()),
                                    authority_ttl_ms=self.config.authority_ttl_ms,
                                    environment=self.config.environment)
        self._authority = authority
        self.authority_slot["context"] = self._authority_context_for(authority)
        return authority

    def _authority_context_for(self, authority: object,
                               *, bootstrap_gate: object | None = None,
                               latency_status: PrivateLatencyStatus | None = None,
                               private_continuity_valid: bool | None = None
                               ) -> ExecutionAuthorityContext:
        return ExecutionAuthorityContext(
            authority=authority,                                   # type: ignore[arg-type]
            recovery_generation=authority.recovery_generation,     # type: ignore[attr-defined]
            market_generation=int(authority.market_generation),    # type: ignore[attr-defined]
            hwm_activation_id=getattr(authority, "hwm_activation_id", None),
            hwm_generation=int(getattr(authority, "hwm_generation", 0)),
            kill_switch_mode=KillSwitchMode.NORMAL,
            bootstrap_gate=bootstrap_gate,                          # type: ignore[arg-type]
            latency_status=latency_status,
            private_continuity_valid=private_continuity_valid)

    def refresh_mark(self, *, seconds: float = 2.0) -> float | None:
        """紧邻写之前刷新正式 MARK（Risk 的 `max_mark_age_ms` 规则不变；不刷新就必然 STALE_MARK）。"""
        self.pump_market(seconds=seconds)
        return self.mark_price()

    def refresh_account_facts(self) -> object:
        """刷新账户快照（readiness 的 available-balance 新鲜度要求；只读 REST）。"""
        return self.private.refresh_snapshot()

    # ------------------------------------------------------------------ 冷启动（既有 bootstrap 机制）

    def activation_eligibility(self, *, latency_status: PrivateLatencyStatus) -> BootstrapEligibility:
        """冷启动资格：全部来自真实事实（不伪造 flat / continuity / open orders）。"""
        reconciliation = self.reconciliation()
        foreign_open = 0
        if reconciliation.get("available"):
            local_ids = {order.client_order_id for order in self.tracker.orders}
            foreign_open = sum(1 for cid in (reconciliation.get("divergence") or {}) if cid not in local_ids)
        return BootstrapEligibility(
            recovery_status=self.recovery.state, market_ready=bool(self._market_evidence is not None
                                                                  and self._market_evidence.ready),
            private_continuity_valid=bool(self.private.continuity_assumed),
            account_flat=abs(self.position_qty()) < 1e-9,
            probex_open_orders=len(self.tracker.active()), foreign_open_orders=foreign_open,
            uncertain_exposure_qty=float(self.tracker.uncertain_exposure()),
            latency_status=latency_status, environment=self.config.environment)

    def activate_bootstrap(self, *, bootstrap_ttl_ms: int, normal_ttl_ms: int) -> BootstrapActivation:
        """用既有 `BootstrapAuthorityCoordinator` 激活冷启动（唯一一次 write attempt 的额度由它拥有）。"""
        result = self._readiness if self._readiness is not None else self.collect_readiness()
        provenance = getattr(self, "_provenance", None)
        if provenance is None:
            raise TestnetError("readiness provenance is missing; collect_readiness() must run first")
        coordinator = BootstrapAuthorityCoordinator(symbol=self.config.symbol, bootstrap_ttl_ms=bootstrap_ttl_ms,
                                                    normal_ttl_ms=normal_ttl_ms)
        activation = coordinator.activate(
            result=result, eligibility=self.activation_eligibility(latency_status=PrivateLatencyStatus.UNOBSERVED),
            provenance=provenance, now_ms=int(self.clock()), environment=self.config.environment)
        self._coordinator = coordinator
        if activation.authority is not None and isinstance(activation.authority, BootstrapAuthority):
            self._authority = activation.authority
            self.authority_slot["context"] = self._authority_context_for(
                activation.authority, bootstrap_gate=BootstrapWriteGate(coordinator),
                latency_status=PrivateLatencyStatus.UNOBSERVED, private_continuity_valid=True)
        return activation

    @property
    def bootstrap_coordinator(self) -> BootstrapAuthorityCoordinator | None:
        return self._coordinator

    @property
    def readiness_result(self) -> LiveReadinessResult | None:
        return self._readiness

    @property
    def authority(self) -> object | None:
        return self._authority

    @property
    def fills_recorded(self) -> int:
        """经 user stream 记录进 FillLedger 的真实成交数（§7）。"""
        return self._fills_recorded

    # ------------------------------------------------------------------ 执行（唯一写路径）

    def submit(self, proposal: OrderProposal, *, now_ms: Milliseconds | None = None) -> ExecutionResult:
        """经 RiskGate 提交（Risk 判定在 engine 内；readiness 由 adapter 写边界校验）。"""
        result = self.engine.submit(proposal, now_ms=int(self.clock()) if now_ms is None else now_ms)
        self._note_updates(result.updates)
        return result

    def cancel(self, client_order_id: str, *, now_ms: Milliseconds | None = None) -> ExecutionResult:
        result = self.engine.cancel(client_order_id, now_ms=int(self.clock()) if now_ms is None else now_ms)
        self._note_updates(result.updates)
        return result

    def _note_updates(self, updates: tuple[object, ...]) -> None:
        """把 owner 产生的状态转换记录为只读事实（供 Activity/Orders trace；不改状态）。"""
        for update in updates:
            order = getattr(update, "order", None)
            if order is None:
                continue
            status = getattr(order, "status", None)
            name = str(getattr(status, "value", status))
            self._transitions.append(LifecycleFact(
                client_order_id=str(order.client_order_id), timestamp=int(order.updated_at),
                event_name=f"OrderStatus:{name}", reason=str(getattr(update, "detail", "") or "")))

    def query_order(self, *, client_order_id: str, timestamp: Milliseconds | None = None) -> tuple[object, ...]:
        """REST query：只用于 recovery / reconciliation / UNKNOWN 收敛（§9）。"""
        return self.connector.query_order(client_order_id=client_order_id,
                                          timestamp=int(self.clock()) if timestamp is None else timestamp)

    # ------------------------------------------------------------------ 产品读模型事实

    def lifecycle_facts(self) -> tuple[LifecycleFact, ...]:
        """订单生命周期事实：owner 产生的状态转换 + 终态/LOST 快照（不新建 store）。"""
        facts = list(self._transitions)
        for order in self.tracker.orders:
            status = getattr(order, "status", None)
            if not (bool(getattr(status, "is_terminal", False)) or bool(getattr(status, "is_lost", False))):
                continue
            name = str(getattr(status, "value", status))
            facts.append(LifecycleFact(client_order_id=str(order.client_order_id),
                                       timestamp=int(order.updated_at), event_name=f"OrderStatus:{name}",
                                       reason="local uncertainty is not a terminal fact" if
                                       bool(getattr(status, "is_lost", False)) else ""))
        return tuple(sorted(facts, key=lambda fact: fact.timestamp))

    def fill_facts(self) -> tuple[FillFact, ...]:
        """成交事实（来自唯一账本 Owner 的 fills；`Fill.order_id` 即 client_order_id）。"""
        return tuple(FillFact(client_order_id=str(fill.order_id), ts=int(fill.exchange_ts),
                              price=float(fill.price), quantity=float(fill.quantity),
                              fee=float(fill.fee), trade_id=str(fill.trade_id))
                     for fill in self.ledger.fills)

    @property
    def latency_observer(self) -> object:
        """执行延迟观测（五阶段；engine 边界 + 私有流 lag）。"""
        return self._latency if self._latency is not None else _NullLatency()

    def product_notes(self) -> tuple[str, ...]:
        """产品 blocker/说明 notes：UNKNOWN 真实原因、reconciliation 状态、user stream 健康（只读事实）。"""
        notes: list[str] = []
        detail = self.adapter.last_submit_detail
        if detail:
            notes.append(f"execution:submit_audit={detail}")
        for record in self._reconciliation_evidence:
            notes.append(f"reconciliation:{record.get('outcome')}:{record.get('client_order_id')}")
        health = self.health()
        notes.append(f"stream:listen_key={health.get('connection_state')}:"
                     f"observed={health.get('private_events_observed')}")
        return tuple(notes)

    # ------------------------------------------------------------------ UNKNOWN 收敛（§9/§10）

    def resolve_unknown(self, client_order_id: str, *, timestamp: Milliseconds | None = None) -> dict[str, object]:
        """UNKNOWN submit 的**唯一**收敛入口：query 真实状态 → 由 tracker 收敛。

        - venue 有该订单 ⇒ 把 query 事实喂给 engine/tracker（唯一 Owner）⇒ 收敛为真实状态；
        - venue 明确没有 / 查询失败 ⇒ **保持 UNKNOWN**（`LOST` + unresolved），**不伪造** REJECTED/ACCEPTED；
        - **不**重试 submit（只读 query）。
        """
        now_ms = int(self.clock()) if timestamp is None else timestamp
        order = self.tracker.require_order(client_order_id)
        events: tuple[object, ...] = ()
        query_error: str | None = None
        try:
            events = tuple(self.connector.query_order(client_order_id=client_order_id, timestamp=now_ms))
        except Exception as exc:  # noqa: BLE001 - 查询失败 ⇒ 仍不确定（不是"不存在"）
            query_error = f"{type(exc).__name__}"
        if events:
            updates = self.manager.on_events(events)          # tracker 唯一 owner
            current = self.tracker.require_order(client_order_id)
            record = {"client_order_id": client_order_id, "outcome": "reconciled_from_query",
                      "venue_events": [type(event).__name__ for event in events],
                      "status": current.status.value, "updates": len(updates), "at_ms": now_ms}
        elif query_error is not None:
            record = {"client_order_id": client_order_id, "outcome": "still_unknown_query_failed",
                      "query_error": query_error, "status": order.status.value, "at_ms": now_ms}
        elif order.status is OrderStatus.PENDING_CANCEL:
            # 撤单 ack 未知：同样只靠 query 收敛（不重发撤单、不假设已撤销）
            record = {"client_order_id": client_order_id, "outcome": "still_unknown_cancel_unconfirmed",
                      "status": order.status.value, "at_ms": now_ms}
        elif order.status is OrderStatus.PENDING_CREATE:
            # venue 明确没有该订单（或超出查询窗口）⇒ 不猜：LOST + unresolved（P0001.6.1 语义）
            self.tracker.mark_lost(client_order_id, timestamp=now_ms,
                                   reason="submit outcome unknown; venue query returned no order")
            self.tracker.note_unresolved_order(client_order_id, reason="unknown submit unresolved")
            record = {"client_order_id": client_order_id, "outcome": "still_unknown_no_venue_record",
                      "status": self.tracker.require_order(client_order_id).status.value, "at_ms": now_ms}
        else:
            record = {"client_order_id": client_order_id, "outcome": "unchanged",
                      "status": order.status.value, "at_ms": now_ms}
        self._reconciliation_evidence.append(record)
        self._reconciliation_facts.append(ReconciliationFact(
            ts=now_ms, identity=client_order_id, identity_kind="client_order_id",
            outcome=str(record.get("outcome", "")),
            reason_code=str(record.get("query_error", "") or ""),
            detail=json_dumps(record)))
        return record

    def reconciliation_facts(self) -> tuple[ReconciliationFact, ...]:
        """reconciliation / UNKNOWN 收敛证据（产品 trace 直接消费）。"""
        return tuple(self._reconciliation_facts)

    def reconciliation_evidence(self) -> tuple[dict[str, object], ...]:
        """UNKNOWN 收敛证据（只读；产品/助手可见）。"""
        return tuple(self._reconciliation_evidence)

    def unknown_submit_pending(self) -> bool:
        """是否存在 UNKNOWN submit 尚未收敛（adapter 计数 > 已收敛数）。"""
        return self.adapter.unknown_submit_count > len(self._reconciliation_evidence)

    # ------------------------------------------------------------------ 只读事实

    def orders(self) -> tuple[Order, ...]:
        return self.tracker.orders

    def open_orders(self) -> tuple[Order, ...]:
        return self.tracker.active()

    def position_qty(self) -> float:
        return float(self.accounting.position(self.config.symbol).qty)

    def mark_price(self) -> float | None:
        return self.accounting.mark_price(self.config.symbol)

    def book_touch(self) -> tuple[float | None, float | None]:
        """真实盘口 best bid / best ask（IOC 定价必须基于它；mark ≠ book）。"""
        history = self.market.history
        if not history:
            return (None, None)
        price = history[-1].price
        return (price.best_bid, price.best_ask)

    def risk_snapshot(self):
        now_ms = int(self.clock())
        return build_risk_snapshot(self.accounting, symbol=self.config.symbol, now_ms=now_ms,
                                   day_start_ts=utc_day_start_ms(now_ms))

    def remaining_budget(self) -> float | None:
        """canonical 预算（Risk domain owner，P0001.14 §3）。"""
        return remaining_exposure_budget(self.risk_snapshot(),
                                        self.config.risk_policy.to_limits()).remaining_notional

    def health(self) -> dict[str, object]:
        """private connector health（§11）：不以 `connected=true` 掩盖 user stream/ack/freshness。"""
        return dict(self.connector.health(now_ms=int(self.clock())).view())

    def reconciliation(self) -> dict[str, object]:
        """本地 vs venue 的差异事实（§10）：不偷偷改历史，只暴露 facts。"""
        local = {order.client_order_id: order.status.value for order in self.tracker.orders}
        external = {}
        try:
            for item in self.connector.open_orders():
                external[item.client_order_id] = getattr(item.status, "value", str(item.status))
        except Exception as exc:  # noqa: BLE001 - 外部事实不可用必须如实呈现（不是"没有挂单"）
            return {"available": False, "reason": f"{type(exc).__name__}", "local": local}
        divergence = {cid: {"local": local.get(cid), "venue": status}
                      for cid, status in external.items() if local.get(cid) != status}
        return {"available": True, "local_active": len(self.tracker.active()), "venue_open": len(external),
                "divergence": divergence}

    # ------------------------------------------------------------------ 内部

    def _stream_state(self) -> StreamState:
        """由**真实** runtime 事实构造恢复门输入（不伪造 ACTIVE / continuity）。"""
        return StreamState(listen_key_state=str(self.private.telemetry.listen_key_state),
                           continuity_assumed=bool(self.private.continuity_assumed),
                           boundary_present=self.private.snapshot_boundary is not None)

def build_testnet_stack(
    *,
    config: TestnetConfig,
    clock: Callable[[], Milliseconds] | None = None,
    credentials: ApiCredentials | None = None,
    normalizer: OrderNormalizer | None = None,
) -> TestnetStack:
    """由真实凭据构造 TESTNET 组合（凭据只从调用方/环境读取，不落盘、不打印）。"""
    from connectors.binance.private.auth import ApiCredentials as _Credentials

    credential_offset = ServerTimeOffset()

    now = clock or (lambda: int(time.time() * 1000))
    creds = credentials if credentials is not None else _Credentials.from_env()
    reconnect = ReconnectPolicy(max_attempts=5, base_backoff_ms=500, max_backoff_ms=5_000)

    market = LiveMarketDataRuntime(
        config=LiveMarketDataConfig(
            symbol=config.symbol, depth_speed="100ms", mark_price_speed="1s", depth_limit=100,
            connect_timeout_s=10.0, read_timeout_s=2.0, snapshot_timeout_s=10.0,
            exchange_info_timeout_s=10.0, reconnect=reconnect, resync_cooldown_ms=1_000,
            history_limit=64, max_book_age_ms=config.market_max_mark_age_ms, ws_host=config.ws_host),
        http_client=UrllibJsonClient(base_url=config.rest_base),
        transport_factory=lambda url, timeout_s: connect(url, timeout_s=timeout_s),
        clock=now)

    private_rest = PrivateRestClient(credentials=creds, fetcher=UrllibRestFetcher(),
                                     base_url=config.rest_base, recv_window_ms=5_000, timeout_s=15.0,
                                     offset=credential_offset, clock=now)
    private = PrivateAccountRuntime(
        config=PrivateAccountConfig(symbol=config.symbol, recv_window_ms=5_000,
                                   listen_key_ttl_ms=60 * 60 * 1000, keepalive_interval_ms=30 * 60 * 1000,
                                   connect_timeout_s=10.0, read_timeout_s=2.0, request_timeout_s=10.0,
                                   max_median_private_lag_ms=2_000, latency_sample_limit=256,
                                   reconnect=reconnect),
        rest=private_rest,
        stream=UserStreamClient(transport_factory=lambda url, timeout_s: connect(url, timeout_s=timeout_s),
                                ws_host=config.ws_host),
        credentials=creds, clock=now)

    # P0001.9.7.2 / D-048：真实写之前必须注入由**真实 exchangeInfo 规则**构造的归一化器；
    # 不注入 ⇒ float 精度泄漏（venue `-1111 Precision is over the maximum defined for this asset`）。
    market.load_trading_rules()
    _rules = market.trading_rules
    if _rules is None:
        raise TestnetError("exchangeInfo rules are required before any real write")
    normalizer = normalizer_from_rules(_rules, price_rounding=config.price_rounding,
                                       quantity_rounding=config.quantity_rounding)

    tracker = OrderTracker(session_id=f"testnet-{int(now())}", venue=Venue.BINANCE)
    accounting = AccountingCore(initial_balance=0.0)
    ledger = FillLedger()

    # 与既有无签名/私有路径一致：**带 server-time offset**（签名请求的 timestamp 必须落在 recvWindow 内）
    credential_offset = ServerTimeOffset()
    rest_client = BinanceExecutionRestClient(credentials=creds,
                                             fetcher=UrllibRestFetcher(expose_http_errors=True),
                                             base_url=config.rest_base, recv_window_ms=5_000, timeout_s=10.0,
                                             offset=credential_offset, clock=now)
    # adapter 的 authority 由本 composition 提供（readiness 未签发 ⇒ 写边界 fail closed，§5）
    authority_holder: dict[str, ExecutionAuthorityContext | None] = {"context": None}

    def authority_provider() -> ExecutionAuthorityContext:
        context = authority_holder["context"]
        if context is None:
            raise TestnetError("no execution authority context; readiness must be issued before submit")
        return context

    # 外部事实 provider（既有生产实现）：open_orders / recent_fills 的真实来源，缺省即 UNKNOWN
    from connectors.binance.execution.external_facts import PrivateExternalFactsProvider

    external_facts = PrivateExternalFactsProvider(rest=private_rest, clock=now)
    adapter = BinanceExecutionAdapter(
        rest=rest_client, environment=config.environment, authority_provider=authority_provider,
        rules_provider=lambda: market.trading_rules, symbol=config.symbol,
        local_status_provider=lambda cid: tracker.require_order(cid).status,
        private_read=external_facts,
        normalizer=normalizer, acceptance=config.acceptance)
    connector = BinancePrivateExecutionConnector(adapter=adapter, venue_identity=_binance_venue(config),
                                                symbol=config.symbol, clock=now, runtime=private,
                                                rate_limit_provider=None,
                                                reconciliation_state_provider=None)
    manager = OrderManager(tracker=tracker, adapter=connector)
    def _historical_baseline(now_ms: Milliseconds) -> object | None:
        """受信历史 baseline（既有 income 恢复路径；与 readiness 使用同一事实来源）。"""
        day_start = utc_day_start_ms(now_ms)
        try:
            income = fetch_income_history(private_rest, window_start_ms=day_start, cutoff_ms=now_ms,
                                          page_limit=MAX_PAGE_LIMIT, max_pages=config.income_max_pages)
        except Exception:  # noqa: BLE001 - 取不到 ⇒ 保持 UNKNOWN（Risk 仍 fail closed）
            return None
        return historical_risk_baseline_from_income(income, day_start_ts=day_start, cutoff_ts=now_ms)

    def _exchange_balance(symbol: str) -> object | None:
        observation = private.latest_snapshot
        return None if observation is None else exchange_available_balance(observation)

    from pathlib import Path as _Path

    store_path = _Path(config.state_dir) / "high_watermark.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    hwm_store = JsonHighWatermarkStore(store_path)
    high_watermark = HighWatermarkTracker(state=hwm_store.load(), store=hwm_store)

    engine = ExecutionEngine(accounting=accounting, gate=RiskGate(config.risk_policy.to_limits()),
                             manager=manager, book_healthy=True, normalizer=normalizer,
                             historical_baseline_provider=_historical_baseline,
                             exchange_available_balance_provider=_exchange_balance,
                             high_watermark_provider=lambda: high_watermark.evidence())
    recovery = StartupRecovery(rest=private_rest, tracker=tracker, accounting=accounting,
                              symbol=config.symbol, clock=now)
    recovery.bind(private)

    from pathlib import Path

    from pathlib import Path as _Path

    store_path = _Path(config.state_dir) / "high_watermark.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    hwm_store = JsonHighWatermarkStore(store_path)
    high_watermark = HighWatermarkTracker(state=hwm_store.load(), store=hwm_store)
    from runtime.latency_observer import BoundedLatencyLog, ExecutionLatencyObserver

    stack = TestnetStack(
        config=config, market=market, private=private, private_rest=private_rest, adapter=adapter,
        connector=connector, tracker=tracker, manager=manager, engine=engine, accounting=accounting,
        ledger=ledger, recovery=recovery, high_watermark=high_watermark, clock=now,
        gate=LiveReadinessGate(policy=config.readiness_policy))
    stack.authority_slot = authority_holder
    stack._latency = ExecutionLatencyObserver(log=BoundedLatencyLog(), clock=now)   # noqa: SLF001
    stack._runtime_id = f"testnet-{int(now())}"                                     # noqa: SLF001
    stack._started_at = int(now())                                                  # noqa: SLF001
    engine.latency_observer = lambda kind, ts: stack._latency.note(kind, ts)        # noqa: SLF001
    return stack


def build_testnet_product_service(stack: TestnetStack, *, host: str = "127.0.0.1", port: int = 0) -> object:
    """把 TESTNET stack 接入 `ProductService`（只读事实；不新增写能力）。

    产品层只消费 Owner 事实：market/tracker/accounting/readiness/connector health/correlation/latency。
    未接线的能力（prediction / maker decision / bounded market history）**如实 UNKNOWN/ABSENT**。
    """
    from connectors.binance.market_connector import BinanceMarkPriceReferenceSource
    from domain.instruments import InstrumentRegistry, PriceType, instrument_id_for, perpetual_crypto_spec
    from product.service import ProductService
    from product.types import Fact, RuntimeIdentity, RuntimeMode
    from venue import binance_venue, VenueEnvironment

    now = stack.clock
    venue = binance_venue(environment=VenueEnvironment.TESTNET)
    rules = stack.market.trading_rules
    if rules is None:
        raise TestnetError("trading rules are required to build the product service")
    base, quote = (stack.config.symbol[:-4], stack.config.symbol[-4:])
    spec = perpetual_crypto_spec(instrument_id=instrument_id_for(stack.config.symbol, venue_id=venue.venue_id),
                                 symbol=stack.config.symbol, base_asset=base, quote_asset=quote,
                                 settlement_asset=quote, price_tick=float(rules.tick_size),
                                 quantity_step=float(rules.step_size), min_quantity=float(rules.min_qty),
                                 min_notional=float(rules.min_notional))
    registry = InstrumentRegistry(instruments=(spec,), current_id=spec.instrument_id)
    mark_source = BinanceMarkPriceReferenceSource(runtime=stack.market, venue_identity=venue,
                                                 instrument_id=spec.instrument_id)

    def reference_price() -> object:
        return mark_source.latest(now_ms=int(now()))

    def market_state() -> object | None:
        history = stack.market.history
        return history[-1] if history else None

    def readiness() -> object | None:
        return stack.readiness_result

    def adapter_audit() -> object | None:
        outcome = stack.adapter.last_submit_outcome
        if outcome is None:
            return None
        from types import SimpleNamespace

        # 注意：产品层用属性访问读取 Owner 事实 ⇒ 返回带属性的对象（不是 dict）
        return SimpleNamespace(classification=str(outcome.classification.value),
                               detail=stack.adapter.last_submit_detail)

    def health() -> dict[str, object]:
        connector = stack.health()
        return {"notes": (f"mode={stack.config.environment.value}",
                          f"private_connector={connector.get('connection_state')}",
                          f"stream_observed={connector.get('private_events_observed')}"),
                "runtime_state": "RUNNING",
                "private_stream_state": connector.get("connection_state")}

    service = ProductService(
        identity=RuntimeIdentity(mode=RuntimeMode.TESTNET, environment="testnet", venue=venue.venue_id,
                                 symbol=stack.config.symbol, runtime_id=stack._runtime_id,  # noqa: SLF001
                                 started_at=int(stack._started_at),  # noqa: SLF001
                                 data_timestamp=Fact.of(int(now()))),
        market_state=market_state,
        prediction=lambda: None,                       # 未接线 ⇒ 如实 UNKNOWN/ABSENT
        maker_decision=lambda: None,
        risk_snapshot=stack.risk_snapshot,
        risk_limits=lambda: stack.config.risk_policy.to_limits(),
        tracker=lambda: stack.tracker,
        accounting=lambda: stack.accounting,
        readiness=readiness,
        authority_id=lambda: getattr(stack.authority, "authority_id", None),
        health=health,
        risk_rejects=lambda: tuple(str(getattr(getattr(r, "reason_code", None), "value", ""))
                                   for r in stack.engine.rejections),
        execution_events=stack.lifecycle_facts,
        clock=now,
        prediction_fresh=lambda: None,
        instrument=lambda: spec,
        instrument_registry=lambda: registry,
        venue=lambda: venue,
        reference_price=reference_price,
        market_connector_health=lambda: None,
        private_connector_health=lambda: stack.connector.health(now_ms=int(now())),
        orders_for_decision=lambda decision_id: tuple(stack.tracker.orders_for_decision(decision_id)),
        fills=stack.fill_facts,
        recent_fill_limit=50,
        risk_decisions=lambda: tuple(getattr(stack.engine, "decision_log", ()) or ()),
        normalization_evidence=lambda: (),
        reconciliation_events=stack.reconciliation_facts,
        ack_latency=lambda: stack.latency_observer,
        adapter_audit=adapter_audit,
        orchestrator_notes=stack.product_notes,
    )
    service.replay_control = lambda: None
    return service


def json_dumps(value: object) -> str:
    """canonical-ish 紧凑 JSON（只用于只读 evidence 展示）。"""
    try:
        return _json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


def _binance_venue(config: TestnetConfig):
    from venue import binance_venue, VenueEnvironment

    return binance_venue(environment=VenueEnvironment.TESTNET)


__all__ = ["TestnetConfig", "TestnetError", "TestnetStack", "build_testnet_stack"]
