"""产品装配入口（closure Slice 1 / F-01）：把仓库里已有的能力**真正装起来并启动**。

唯一边界：本模块负责 **assembly**，不拥有任何交易事实：

    resolved config + ConfigSnapshot
        + JsonRunRegistry
        + RuntimeSession / SessionHost
        + ProductService（market/account/execution 等投影经 provider 注入）
        + AssistantService
        + ActionGateway（基线 handler）
        → API / UI server（默认 REPLAY，默认 127.0.0.1，LIVE 写权限仍走原 readiness/authority 路径）

约束：

- **默认 mode = REPLAY**；TESTNET/LIVE 不是默认启动路径，必须显式 `--mode`；
- LIVE 写能力不因装配而改变：本模块不注册任何写 handler，CAPITAL action 结构上不可用；
- 数值（投影上限、policy 等）必须由调用方显式给出，本模块**不提供任何业务默认值**；
- Product 层只读：所有事实经 provider 注入，`product/` 不反向 import 交易域。
"""

from __future__ import annotations

import argparse
import json
import dataclasses
from types import SimpleNamespace
import os
import signal
import threading
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from dataclasses import dataclass, field
from dataclasses import replace as dataclasses_replace

from actions import (
    ActionAuditLog,
    ActionContext,
    ActionGateway,
    ActionRequest,
    ConfirmationRegistry,
    HandlerResult,
)
from api.server import create_server
from assistant import AssistantService
from product.provenance import (ConfigEntry, ConfigSource, build_config_snapshot,
                                env_config_entries, resolve_config)
from product.service import ProductService
from market.events.types import Venue
from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports import build_run_summary
from reports.json import summary_to_json
from reports.types import RunStatus
from runtime.observability import configure_logging, log_event, register_secret
from runtime.session import RuntimeSession, SessionSummaryFacts
from runtime.state import RuntimeState, RuntimeStatus, RuntimeStatusTracker
from runtime.wiring import SessionHost
from domain.instruments import (AssetClass, InstrumentRegistry, PriceType, ProductType,
                                instrument_id_for, perpetual_crypto_spec)
from connectors.paper import PaperExecutionConnector, PaperMarketDataConnector
from risk.budget import remaining_exposure_budget
from venue import (MarkPriceReferenceSource, ReferencePriceProvider, VenueEnvironment, paper_venue,
                   venue_for_mode)
from runtime.maker_config import build_loop_options, build_maker_policy
from storage.retention import RetentionPolicy, prune_finished_runs
from storage.run_registry import (DEFAULT_RUN_REGISTRY_DIR, INDEX_FILE, RUNS_DIR, RUN_REGISTRY_ENV,
                                  JsonRunRegistry, process_start_epoch_ms)

DEFAULT_HOST = "127.0.0.1"


#: 常见 quote asset（用于把 symbol 拆成 base/quote；拆不出 ⇒ 不猜，交由调用方决定）
_QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH")


def _split_symbol(symbol: str) -> tuple[str, str]:
    """`BTCUSDT` → (`BTC`, `USDT`)；无法确定 ⇒ (`symbol`, "")（不猜）。"""
    upper = symbol.upper()
    for quote in _QUOTE_ASSETS:
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)], quote
    return upper, ""


@dataclass(frozen=True, slots=True)
class _CallableClock:
    """既有 `Clock` 协议适配（`now()`）；时间仍由 profile 注入。"""

    now_fn: Callable[[], int]

    def now(self) -> int:
        return int(self.now_fn())


@dataclass(frozen=True, slots=True)
class _ReconciliationFact:
    """最小只读事实（供既有 projection 投影；不实现 reconciliation 逻辑）。"""

    actions: tuple[object, ...]
    corrective: tuple[object, ...]
    converged: bool


@dataclass(frozen=True, slots=True)
class _TraceEvent:
    """F-08：受控入口产生的只读 trace 事实（不是第二套证据库，只是每次调用的记录）。"""

    ts: int
    identity: str
    identity_kind: str
    outcome: str
    reason_code: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _LifecycleFact:
    """F-08：订单生命周期事实（字段搬运自 OrderTracker，不新建 event store）。"""

    client_order_id: str
    timestamp: int
    event_name: str
    reason: str = ""

    @property
    def event_type(self) -> str:
        """F5：raw-fact schema 使用 `event_type`（与 trace 的 `event_name` 同一事实）。"""
        return self.event_name


@dataclass(frozen=True, slots=True)
class _FillFact:
    """F-08：成交事实（字段搬运自 FillLedger；`order_id` 即 client_order_id）。"""

    client_order_id: str
    ts: int
    price: float
    quantity: float
    fee: float
    trade_id: str
#: 允许的启动模式（默认 REPLAY；TESTNET/LIVE 必须显式指定）
ALLOWED_MODES = (RuntimeMode.REPLAY, RuntimeMode.PAPER, RuntimeMode.TESTNET, RuntimeMode.LIVE)
#: loopback bind（不强制认证）；其它 bind 必须显式 opt-in + bearer token
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def is_loopback_host(host: str) -> bool:
    """判断 bind host 是否 loopback（fail closed：无法判定时按非 loopback 处理）。"""
    import ipaddress

    text = str(host or "").strip().lower()
    if text in LOOPBACK_HOSTS or text.startswith("127."):
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def resolve_secret_ref(ref: str, environ: Mapping[str, str]) -> str:
    """解析 secret 引用（当前只支持 `env:NAME`）；缺失 ⇒ fail closed（不静默继续）。"""
    if not isinstance(ref, str) or not ref:
        raise AssemblyError("secret reference must be a non-empty string")
    if ref.startswith("env:"):
        name = ref[len("env:"):]
        value = environ.get(name)
        if not value:
            raise AssemblyError(f"secret reference {ref!r} is not present in the environment (fail closed)")
        return str(value)
    raise AssemblyError(f"unsupported secret reference scheme in {ref!r} (allowed: env:)")


class AssemblyError(RuntimeError):
    """装配契约错误（缺必填输入 / 非法组合）。"""


def clock_now_ms() -> int:
    """装配入口允许使用 wall-clock（它本身就是 live 边界）。"""
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class FeedProfile:
    """真实回放/纸面运行的输入（全部显式；bounds 无默认值）。"""

    event_store: str
    window_ms: int
    bucket_ms: int
    max_points: int
    price_levels: int
    history_capacity: int
    view_depth: int

    def __post_init__(self) -> None:
        for name in ("window_ms", "bucket_ms", "max_points", "price_levels", "history_capacity",
                     "view_depth"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AssemblyError(f"FeedProfile.{name} must be a positive int")
        if not str(self.event_store):
            raise AssemblyError("FeedProfile.event_store must be a path")


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """装配输入（全部显式；本类不提供业务默认值，只提供启动默认：REPLAY + loopback）。"""

    symbol: str
    config_entries: tuple[ConfigEntry, ...]
    venue: str = "binance"
    mode: RuntimeMode = RuntimeMode.REPLAY
    environment: str = "local"
    host: str = DEFAULT_HOST
    port: int = 0
    run_registry_dir: str = DEFAULT_RUN_REGISTRY_DIR
    runtime_id: str | None = None
    #: 显式注入的时钟（测试可替换）；生产使用 wall-clock
    clock: Callable[[], int] = clock_now_ms
    #: 真实 feed（提供后 REPLAY/PAPER 会真正消费事件并产生市场事实）
    feed: FeedProfile | None = None
    #: Batch 3：行情来源（event-store 或 binance-public 真实持续公网行情；只读）
    market_source: str = "event-store"
    #: F-12：non-loopback 必须显式 opt-in + bearer token（缺任一 ⇒ 拒绝启动）
    allow_non_loopback: bool = False
    #: F-12：auth token 的 secret 引用（如 `env:PROBEX_API_TOKEN`）；值永不进 provenance/snapshot/logs
    auth_token_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise AssemblyError("RuntimeProfile.symbol must be a non-empty string")
        if not isinstance(self.mode, RuntimeMode) or self.mode not in ALLOWED_MODES:
            raise AssemblyError(f"RuntimeProfile.mode must be one of {ALLOWED_MODES}")
        if not self.config_entries:
            raise AssemblyError("RuntimeProfile.config_entries must not be empty (no implicit defaults)")
        if not isinstance(self.port, int) or self.port < 0:
            raise AssemblyError("RuntimeProfile.port must be a non-negative int")
        if not isinstance(self.host, str) or not self.host:
            raise AssemblyError("RuntimeProfile.host must be a non-empty string")
        for entry in self.config_entries:
            if not isinstance(entry, ConfigEntry):
                raise AssemblyError("RuntimeProfile.config_entries must be ConfigEntry values")
        if not isinstance(self.allow_non_loopback, bool):
            raise AssemblyError("RuntimeProfile.allow_non_loopback must be a bool")
        if self.auth_token_ref is not None and (
                not isinstance(self.auth_token_ref, str) or not self.auth_token_ref):
            raise AssemblyError("RuntimeProfile.auth_token_ref must be a non-empty string when given")
        if self.market_source not in ("event-store", "binance-public"):
            raise AssemblyError("RuntimeProfile.market_source must be event-store or binance-public")
        # F-12：非 loopback 的启动姿态必须显式（警告后继续是不允许的）
        if not is_loopback_host(self.host):
            if not self.allow_non_loopback:
                raise AssemblyError(
                    "non-loopback bind requires explicit opt-in (--allow-non-loopback); refusing to start")
            if not self.auth_token_ref:
                raise AssemblyError(
                    "non-loopback bind requires an auth token reference (--auth-token-ref env:NAME); "
                    "refusing to start")


@dataclass
class ProductRuntime:
    """一次真实产品运行（assembly + lifecycle + serving）。"""

    profile: RuntimeProfile
    _registry: JsonRunRegistry = field(init=False)
    _cfg: object = field(init=False)
    _session: RuntimeSession = field(init=False)
    _host: SessionHost = field(init=False)
    _service: ProductService = field(init=False)
    _gateway: ActionGateway = field(init=False)
    _assistant: AssistantService = field(init=False)
    _tracker: RuntimeStatusTracker = field(init=False)
    _server: object | None = field(default=None, init=False)
    _identity: RuntimeIdentity = field(init=False)
    _facts: SessionSummaryFacts = field(init=False)
    _history: object | None = field(default=None, init=False)
    _feed_stats: dict[str, object] = field(default_factory=dict, init=False)
    _feed_provider: object | None = field(default=None, init=False)
    _projection_config: object | None = field(default=None, init=False)
    _safety_policy: object | None = field(default=None, init=False)
    _limit_definition: object | None = field(default=None, init=False)
    _usage: object | None = field(default=None, init=False)
    _trading_rules: object | None = field(default=None, init=False)
    _tracker_owner: object | None = field(default=None, init=False)
    _latency_log: object | None = field(default=None, init=False)
    _safety_projection: object | None = field(default=None, init=False)
    _accounting: object | None = field(default=None, init=False)
    _account_timeline: object | None = field(default=None, init=False)
    _accounting_provider: object | None = field(default=None, init=False)
    _usage_collector: object | None = field(default=None, init=False)
    #: F-13：**唯一**解析结果（CLI > ENV > FILE > CONSTRUCTOR）；装配只消费它
    _resolved_config: tuple[ConfigEntry, ...] = field(default_factory=tuple, init=False)
    _auth_token: str | None = field(default=None, init=False)
    _prediction_runtime: object | None = field(default=None, init=False)
    _private_runtime: object | None = field(default=None, init=False)
    _latency_observer: object | None = field(default=None, init=False)
    _execution: object | None = field(default=None, init=False)
    _risk_limits: object | None = field(default=None, init=False)
    _maker_policy: object | None = field(default=None, init=False)
    _decision_loop: object | None = field(default=None, init=False)
    # P0001.15：instrument / venue / reference price / connector 事实
    _instrument_registry: object | None = field(default=None, init=False)
    _venue_identity: object | None = field(default=None, init=False)
    _reference_prices: object | None = field(default=None, init=False)
    _market_connector: object | None = field(default=None, init=False)
    _execution_connector: object | None = field(default=None, init=False)
    _simulated_venue: object | None = field(default=None, init=False)
    _reconciliation_events: list[object] = field(default_factory=list, init=False)
    _retention_policy: RetentionPolicy = field(default_factory=RetentionPolicy, init=False)
    _last_prune: dict[str, object] | None = field(default=None, init=False)
    _readiness_provider: object | None = field(default=None, init=False)
    _feed_error: str | None = field(default=None, init=False)
    #: Batch 3 (G-A2/G-B2)：真实公网行情泵（只读；与 event-store feed 二选一）
    _public_pump: object | None = field(default=None, init=False)
    _market_source: str = field(default="event-store", init=False)

    def __post_init__(self) -> None:
        self._local_data_clock: dict[str, int | None] = {"ms": None}
        self._market_source = self.profile.market_source
        # P0001.17：run 级事实持久化（market 时间线 + decision/order/fill）；按 bucket 节流
        self._persisted_market_ts: int | None = None
        if self.profile.feed is not None:
            # P0001.17 §3：本地 Replay/PAPER 的时间必须与**市场数据时间**一致（确定性、可复现），
            # 否则引擎订单时间（wall clock）与市场/模拟成交时间（event time）不在同一时间轴，
            # 会导致撤单永不确认、模拟成交错时。数据时间在首个事件到达后可用（此前回退 wall clock）。
            from dataclasses import replace as _replace

            wall = self.profile.clock
            data_clock = self._local_data_clock

            def _local_clock() -> int:
                data_ms = data_clock["ms"]
                return int(data_ms) if data_ms is not None else int(wall())

            self.profile = _replace(self.profile, clock=_local_clock)
        now = int(self.profile.clock())
        self._registry = JsonRunRegistry(pathlib_path(self.profile.run_registry_dir))
        # F-13：唯一次解析（provenance resolver）；后续所有消费方只读这个结果
        resolved = list(resolve_config(self.profile.config_entries))
        # F-12：auth token **只记录引用名**（值永不进 snapshot / provenance）
        if self.profile.auth_token_ref:
            from product.provenance import secret_entry

            resolved.append(secret_entry("api.auth_token", source=ConfigSource.ENV,
                                         secret_ref=self.profile.auth_token_ref))
        self._resolved_config = tuple(resolved)
        self._cfg = build_config_snapshot(config_id=f"runtime-{self.profile.mode.value.lower()}",
                                         entries=self._resolved_config, created_at=now)
        # token 只在**非 loopback**时强制；值登记到 redactor，任何日志/异常都会遮蔽它
        if self.profile.auth_token_ref:
            self._auth_token = resolve_secret_ref(self.profile.auth_token_ref, os.environ)
            register_secret(self._auth_token)
        # F-15：retention 全部显式（缺键 ⇒ 该项 UNBOUNDED，不偷偷删）
        self._retention_policy = self._build_retention_policy()
        self._tracker = RuntimeStatusTracker(mode=self.profile.mode)
        self._identity = RuntimeIdentity(
            mode=self.profile.mode, environment=self.profile.environment, venue=self.profile.venue,
            symbol=self.profile.symbol,
            runtime_id=self.profile.runtime_id or f"{self.profile.mode.value.lower()}-{now}",
            started_at=now, data_timestamp=Fact.unknown("no market data consumed yet"))
        self._session = RuntimeSession(mode=self.profile.mode, environment=self.profile.environment,
                                       venue=self.profile.venue, symbol=self.profile.symbol,
                                       registry=self._registry, clock=self.profile.clock,
                                       config=self._cfg, runtime_id=self._identity.runtime_id)
        self._host = SessionHost(session=self._session, facts_provider=self._facts_provider)
        self._facts = SessionSummaryFacts()
        self._gateway = ActionGateway(clock=self.profile.clock,
                                      confirmations=ConfirmationRegistry(ttl_ms=30_000),
                                      audit=ActionAuditLog(capacity=500))
        self._service = self._build_service()
        self._assistant = AssistantService(snapshot_provider=lambda: self._service.snapshot(),
                                           gateway=self._gateway)
        self._register_baseline_handlers()

    # ------------------------------------------------------------------ property

    @property
    def identity(self) -> RuntimeIdentity:
        return self._identity

    @property
    def run_id(self) -> str:
        return self._session.run_id

    @property
    def status(self) -> RuntimeStatus:
        return self._tracker.status()

    @property
    def service(self) -> ProductService:
        return self._service

    @property
    def gateway(self) -> ActionGateway:
        return self._gateway

    @property
    def assistant(self) -> AssistantService:
        return self._assistant

    @property
    def loopback(self) -> bool:
        """是否 loopback bind（F-12）。"""
        return is_loopback_host(self.profile.host)

    @property
    def auth_required(self) -> bool:
        """非 loopback ⇒ 所有 /api/v1/* 需要 bearer token；loopback 不强制。"""
        return not self.loopback

    @property
    def auth_token(self) -> str | None:
        """仅在需要认证时暴露给 server；**不得**写入任何 snapshot/log。"""
        return self._auth_token if self.auth_required else None

    # ------------------------------------------------------------------ assembly

    def _facts_provider(self) -> SessionSummaryFacts:
        return self._build_summary_facts()

    def _build_summary_facts(self) -> SessionSummaryFacts:
        """F4：从**既有 Owner** 汇总 run summary 输入（不重算指标、不新建 accounting/risk owner）。

        - equity 采样 ⇒ `BoundedAccountTimeline`（真实采样）；
        - fees / realized / unrealized / 成交数 / 最终仓位 ⇒ 既有 `AccountingCore`；
        - orders ⇒ 既有 `OrderTracker`；risk rejects ⇒ 既有 `ExecutionEngine.rejections`。
        取不到的字段保持 None（⇒ 指标 UNKNOWN，**绝不伪造**）。
        """
        from reports.metrics import EquitySample

        def scalar(target: object, name: str) -> float | None:
            value = getattr(target, name, None)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)

        samples: tuple[EquitySample, ...] = ()
        if self._account_timeline is not None:
            samples = tuple(
                EquitySample(ts=int(sample.ts), equity=float(sample.equity))
                for sample in self._account_timeline.samples()
                if getattr(sample, "equity", None) is not None)
        accounting = self._accounting
        fees = realized = unrealized = final_position = fills = None
        if accounting is not None:
            fees = scalar(accounting, "trading_fees")
            realized = scalar(accounting, "realized_trade_pnl")
            unrealized = scalar(accounting, "unrealized_pnl")
            try:
                final_position = scalar(accounting.position(self.profile.symbol), "qty")
            except Exception:  # noqa: BLE001 - 取不到 ⇒ UNKNOWN
                final_position = None
            ledger = getattr(accounting, "fills", None)
            count = getattr(ledger, "count", None)
            fills = int(count) if isinstance(count, int) and not isinstance(count, bool) else None
        orders = (tuple(getattr(self._tracker_owner, "orders", ()))
                  if self._tracker_owner is not None else ())
        risk_rejects: tuple[str, ...] = ()
        if self._execution is not None:
            risk_rejects = tuple(
                str(getattr(getattr(rejection, "reason_code", None), "value", ""))
                for rejection in self._execution.rejections)
        anomalies = (() if self._feed_error is None else (f"feed:{self._feed_error}",))
        facts = SessionSummaryFacts(orders=orders, fills=fills, fees=fees, realized_pnl=realized,
                                    unrealized_pnl=unrealized, final_position=final_position,
                                    equity_samples=samples, risk_rejects=risk_rejects,
                                    anomalies=anomalies)
        self._facts = facts
        return facts

    def _durable_run_summary(self, run_id: str | None) -> dict[str, object] | None:
        """F4：从 durable run record 构建 RunSummary payload（既有 report builder，不重算指标）。

        unknown run ⇒ None（API 映射 404）；已知 run 但缺 facts ⇒ 由 builder 产出全 UNKNOWN 字段
        （**不**把整端点变成 503）。
        """
        from reports.json import summary_to_jsonable

        if not run_id:
            runs = self._registry.list()
            if not runs:
                return None
            run_id = runs[0].run_id
        record = self._registry.load(str(run_id))
        if record is None:
            return None
        if isinstance(record.summary, dict):
            return record.summary
        summary = build_run_summary(
            identity=record.runtime, run_id=record.run_id, started_at=record.started_at,
            ended_at=(int(record.ended_at.value) if record.ended_at.known else None),
            config_id=(str(record.config_id.value) if record.config_id.known else None))
        return summary_to_jsonable(summary)

    def _build_retention_policy(self) -> RetentionPolicy:
        """从 `retention.*` 构造显式 policy（没有任何隐式生产数值）。"""
        values = self._config_values()
        known: dict[str, int] = {}
        for key, field_name in (("retention.run_max_runs", "run_max_runs"),
                                ("retention.run_max_age_ms", "run_max_age_ms"),
                                ("retention.event_store_max_bytes", "event_store_max_bytes"),
                                ("retention.log_max_bytes", "log_max_bytes"),
                                ("retention.log_backup_count", "log_backup_count")):
            if key in values:
                known[field_name] = int(values[key])
        return RetentionPolicy(**known)  # type: ignore[arg-type]

    @property
    def retention_policy(self) -> RetentionPolicy:
        return self._retention_policy

    def attach_readiness(self, provider: object) -> "ProductRuntime":
        """显式接入既有 readiness gate 的只读结果（F-15：不新增业务判断）。"""
        self._readiness_provider = provider
        self._service.readiness = provider  # type: ignore[assignment]
        return self

    def _current_run_id(self) -> str | None:
        """当前 run id；session 尚未 start ⇒ None（不抛错）。"""
        try:
            return self._session.run_id
        except Exception:  # noqa: BLE001 - session 未启动
            return None

    def _readiness_result(self) -> object | None:
        provider = self._readiness_provider
        if provider is None:
            return None
        try:
            return provider()  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - 读不到就是 NOT_EVALUATED，不是 READY
            return None

    def run_retention(self, *, now_ms: int | None = None) -> object:
        """执行一次显式 retention（只删已完成旧 run；active run 永不删）并记录日志。"""
        now = int(self.profile.clock()) if now_ms is None else int(now_ms)
        report = prune_finished_runs(self._registry, self._retention_policy, now_ms=now)
        self._last_prune = report.to_payload()
        log_event("storage", "retention_prune",
                  runtime_id=self._identity.runtime_id, run_id=self._current_run_id(),
                  mode=self.profile.mode.value,
                  reason_code=("RETENTION_PRUNED" if report.removed_run_ids else "RETENTION_NOOP"),
                  applied=report.applied, removed=len(report.removed_run_ids),
                  removed_record_files=report.removed_record_files, kept_runs=report.kept_runs,
                  skipped_active_run_id=report.skipped_active_run_id,
                  bounded=not report.policy.is_unbounded)
        return report

    def ops_posture(self) -> object | None:
        """F-12/F-15：operational posture（network/auth/logging/retention + 四层健康）。"""
        from runtime.observability import logging_posture
        from runtime.ops import (NetworkPosture, build_health_split, build_ops_payload,
                                 build_retention_posture)

        now = int(self.profile.clock())
        status = self._tracker.status()
        readiness = self._readiness_result()
        readiness_status = None
        readiness_reasons: tuple[str, ...] = ()
        if readiness is not None:
            raw_status = getattr(readiness, "status", None)
            readiness_status = str(getattr(raw_status, "value", raw_status))
            readiness_reasons = tuple(str(getattr(reason, "value", reason))
                                      for reason in (getattr(readiness, "reasons", ()) or ()))
        execution_health = None
        if self._safety_projection is not None:
            execution_health = self._safety_projection.health().status.value  # type: ignore[attr-defined]
        health = build_health_split(runtime_state=status.state.value, runtime_detail=status.detail,
                                    readiness_status=readiness_status,
                                    readiness_reasons=readiness_reasons,
                                    execution_health=execution_health)
        logging_payload = logging_posture().to_payload()
        runs = self._registry.list()
        index_path = self._registry.root / INDEX_FILE
        runs_dir = self._registry.root / RUNS_DIR
        record_bytes = sum(path.stat().st_size for path in runs_dir.glob("*.json"))
        event_store_bytes = None
        if self.profile.feed is not None:
            event_path = pathlib_path(self.profile.feed.event_store)
            if event_path.exists():
                event_store_bytes = event_path.stat().st_size
        retention = build_retention_posture(
            policy=self._retention_policy, runs_total=len(runs),
            index_bytes=(index_path.stat().st_size if index_path.exists() else 0),
            record_bytes=record_bytes, event_store_bytes=event_store_bytes,
            audit_entries=len(self._gateway.audit.entries()),
            audit_capacity=self._gateway.audit.capacity,
            latency_samples=len(self._latency_log.samples()),
            latency_capacity=self._latency_log.capacity,
            logging_bounded=bool(logging_payload.get("bounded")), last_prune=self._last_prune)
        network = NetworkPosture(bind_host=self.profile.host, loopback=self.loopback,
                                 allow_non_loopback=self.profile.allow_non_loopback,
                                 auth_required=self.auth_required,
                                 auth_token_ref=self.profile.auth_token_ref)
        return build_ops_payload(process_started_at_ms=process_start_epoch_ms(os.getpid()),
                                 network=network, health=health, logging=logging_payload,
                                 retention=retention, now_ms=now)

    def _config_values(self) -> dict[str, object]:
        """已解析的非敏感配置值（**只消费 resolver 输出**，不重新解析一套配置）。"""
        return {entry.name: entry.value.value
                for entry in self._resolved_config if entry.value.known}

    def _reconciliation_state(self) -> object | None:
        """既有 reconciliation 事实的只读投影；未运行过 ⇒ None（UNKNOWN）。"""
        if not getattr(self, "_reconciled_count", 0):
            return None
        return _ReconciliationFact(actions=(), corrective=(), converged=True)

    def _accounting_health(self) -> object | None:
        """accounting 健康度（只投影既有事实：equity/balance 是否已知）。"""
        accounting = self._accounting
        if accounting is None:
            return None
        from product.types import Fact

        equity = getattr(accounting, "equity", None)
        return Fact.of("HEALTHY" if equity is not None else "UNKNOWN")

    def _latency_note(self, kind: str, ts_ms: int) -> None:
        """engine 边界 → observer（decision_ready 表示决策边界已到）。"""
        observer = getattr(self, "_latency_observer", None)
        if observer is None:
            return
        if kind == "decision_ready":
            observer.note_decision(int(ts_ms))
        else:
            observer.note(kind, int(ts_ms))

    def run_paper_smoke_order(self, *, price: float, quantity: float) -> dict[str, object]:
        """最小真实执行场景（**仅 REPLAY/PAPER**）：经既有 engine+PaperBroker 提交一笔 post-only 并撤销。

        用途：让 `submit_to_ack` / `cancel_to_ack` / `decision_to_submit` 接到**真实边界**并产生真实样本。
        不产生策略行为、不用于 TESTNET/LIVE、不是产品控制面。
        """
        if self.profile.mode not in (RuntimeMode.REPLAY, RuntimeMode.PAPER):
            raise AssemblyError("paper smoke order is only available for replay/paper runtimes")
        if self._execution is None:
            raise AssemblyError("execution chain is not wired")
        from risk.types import OrderProposal
        from portfolio.types import Side

        now = int(self.profile.clock())
        self._latency_note("decision_ready", now)
        proposal = OrderProposal(symbol=self.profile.symbol, side=Side.BUY, quantity=float(quantity),
                                 price=float(price), post_only=True)
        submitted = self._execution.submit(proposal, now_ms=now)
        order = submitted.order
        if order is None:
            return {"submitted": False, "reason": "rejected"}
        # ack 已在 submit 路径经 engine 处理；这里撤销并记录真实延迟样本。
        # P0001.17：本地事件级模拟可能已经把该单成交（终态）⇒ 不做无意义撤销，如实报告终态。
        current = getattr(order, "status", None)
        if current is not None and bool(getattr(current, "is_terminal", False)):
            return {"submitted": True, "client_order_id": order.client_order_id,
                    "cancelled": False, "terminal_status": str(getattr(current, "value", current)),
                    "observed": dict(self._latency_observer.observed)}
        cancelled = self._execution.cancel(order.client_order_id, now_ms=now)
        return {"submitted": True, "client_order_id": order.client_order_id, "cancelled": True,
                "cancel_updates": len(cancelled.updates),
                "observed": dict(self._latency_observer.observed)}

    def _usage_snapshot(self) -> object | None:
        """当前用量事实：来自真实响应头采集；检查过但无用量头 ⇒ 仍给出“无值快照”（带来源与观测时间）。"""
        collector = getattr(self, "_usage_collector", None)
        if collector is None:
            return None
        collected = collector.snapshot()
        if collected is not None:
            return collected
        if collector.observations:
            last = collector.observations[-1]
            from execution_safety.venue import VenueUsageSnapshot

            return VenueUsageSnapshot(used_weight=None, used_orders=None, reset_at_ms=None,
                                      source=(f"inspected {len(collector.observations)} response(s); "
                                              f"no usage headers (last: {last.endpoint})"),
                                      observed_at_ms=last.observed_at_ms)
        return None

    def usage_evidence(self) -> dict[str, object]:
        """采集证据（供验收/审计：证明 collector 真实运行过并检查过响应头）。"""
        collector = getattr(self, "_usage_collector", None)
        return {} if collector is None else collector.evidence()

    def usage_probe(self, *, url: str, timeout_s: float = 10.0) -> dict[str, object]:
        """真实 usage 采集 smoke：对一个真实 endpoint 发一次请求，检查响应头里是否有用量事实。"""
        import urllib.request

        collector = getattr(self, "_usage_collector", None)
        if collector is None:
            raise AssemblyError("usage collector is not initialised")
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as handle:
                handle.read()
                headers = dict(handle.headers.items())
        except Exception as exc:  # noqa: BLE001 - 网络不可用也必须如实记录（不得伪造成“已检查”）
            return {"probed": False, "error": type(exc).__name__, "evidence": collector.evidence()}
        observation = collector.observe(endpoint=url.split("?")[0].split("/", 3)[-1] or url, headers=headers)
        return {"probed": True, "headers_seen": list(observation.header_names),
                "usage": None if observation.usage is None else {
                    "used_weight": observation.usage.used_weight,
                    "used_orders": observation.usage.used_orders,
                    "source": observation.usage.source},
                "note": observation.note, "evidence": collector.evidence()}

    def attach_usage_fetcher(self, fetcher: object) -> object:
        """把真实 fetcher 绑到 usage collector（TESTNET/LIVE 路径）；返回可直接使用的包装 fetcher。"""
        collector = getattr(self, "_usage_collector", None)
        if collector is None:
            raise AssemblyError("usage collector is not initialised")
        return collector.bind(fetcher)

    def attach_prediction_runtime(self, runtime: object) -> "ProductRuntime":
        """接入既有 PredictionRuntime（只读其 status/health；不改变预测语义）。"""
        self._prediction_runtime = runtime
        return self

    def attach_private_runtime(self, runtime: object) -> "ProductRuntime":
        """接入既有私有流 runtime（只读 telemetry；private latency 由此成为真实事实）。"""
        self._private_runtime = runtime
        return self

    def _prediction_provider_status(self) -> object | None:
        """PredictionRuntime 的**真实** provider 状态（既有 `status_at` 契约）。"""
        runtime = getattr(self, "_prediction_runtime", None)
        if runtime is None:
            return None                                        # 未 attach ⇒ 由 Product 层给 UNKNOWN + reason
        try:
            status = runtime.status_at(int(self.profile.clock()))
        except Exception as exc:  # noqa: BLE001 - 取不到状态不得崩
            return f"UNKNOWN({type(exc).__name__})"
        return getattr(status, "value", status)

    def _private_latency_state(self) -> object | None:
        runtime = getattr(self, "_private_runtime", None)
        if runtime is None:
            return None
        telemetry = getattr(runtime, "telemetry", None)
        distribution = getattr(telemetry, "private_lag_ms", None) if telemetry is not None else None
        if distribution is None:
            # 已接线但当前环境没有产生可测事件 ⇒ 事实为 UNOBSERVED（与"未接线"的 None 区分开）
            return "UNOBSERVED"
        median = getattr(distribution, "median", None)
        if median is None:
            return None
        raw = getattr(telemetry, "last_raw_receive_lag_ms", None)
        calibration = getattr(telemetry, "clock_calibration", None)
        offset = getattr(calibration, "offset_ms", None)
        if raw is not None and offset is not None:
            self._latency_observer.note_private_event_lag(raw_lag_ms=int(raw), offset_ms=int(offset))
        return "HEALTHY" if int(median) >= 0 else "DEGRADED"

    def _exposure(self) -> dict[str, object]:
        """既有 OrderTracker 的暴露事实（只读；不新增任何风险计算）。"""
        tracker = self._tracker_owner
        lost = [order for order in tracker.orders
                if getattr(getattr(order, "status", None), "is_lost", False)]
        return {"uncertain_exposure": float(tracker.uncertain_exposure()),
                "unknown_orders": len(lost), "lost_count": len(lost)}

    def _build_service(self) -> ProductService:
        """组装 ProductService：只注入真实存在的 provider；没有的一律保持 UNKNOWN。"""
        from execution.tracker import OrderTracker
        from execution_safety.latency import BoundedLatencyLog, LatencySample
        from execution_safety.projection import ExecutionSafetyProjection
        from runtime.safety_config import (build_limit_definition, build_safety_policy,
                                           build_trading_rules, build_usage_snapshot)

        values = self._config_values()
        self._safety_policy = build_safety_policy(values)                       # Step 1
        self._trading_rules = build_trading_rules(values, symbol=self.profile.symbol)   # Step 2
        self._limit_definition = build_limit_definition(values)                # Step 2（定义，证据驱动）
        # F-03 收口：usage 只能来自真实响应头采集（禁止 operator 手填当前用量）
        from execution_safety.venue_usage import VenueUsageCollector

        self._usage_collector = VenueUsageCollector(clock=self.profile.clock)
        self._usage = None                                                     # 由 collector 提供
        # 注意：`session.run_id` 只有 start() 之后才可用；组合期使用 runtime identity
        self._tracker_owner = OrderTracker(session_id=self._identity.runtime_id)
        self._latency_log = BoundedLatencyLog(capacity=2_000)
        # Step 3：把 Step 1/2 的 provider 交给**既有** projection（不新增 health 逻辑）
        # Step 4：仅在显式给出 accounting.initial_balance 时构造既有 AccountingCore（不发明余额）
        accounting = timeline = None
        if "accounting.initial_balance" in values:
            from portfolio.accounting import AccountingCore
            from product.account_timeline import BoundedAccountTimeline

            accounting = AccountingCore(initial_balance=float(values["accounting.initial_balance"]))
            capacity = int(values.get("accounting.timeline_capacity", 0) or 0)
            timeline = (BoundedAccountTimeline(capacity=capacity, run_id=self._identity.runtime_id)
                        if capacity > 0 else None)
        provider = None
        if accounting is not None:
            from runtime.accounting_facts import AccountingFactsProvider

            provider = AccountingFactsProvider(
                accounting=accounting, symbol=self.profile.symbol, clock=self.profile.clock,
                exposure_provider=lambda: (float(self._tracker_owner.total_pending_exposure()),
                                           float(self._tracker_owner.confirmed_open_exposure)))
        self._accounting = accounting
        self._accounting_provider = provider
        self._account_timeline = timeline
        # Step 5：真实 reconciliation 采样（受控入口的耗时；其余阶段保持 UNKNOWN）
        self._reconciled_count = 0

        def _request_reconciliation() -> dict[str, object]:
            started = int(self.profile.clock())
            self._reconciled_count += 1
            self._latency_observer.note_reconciliation_duration(started)
            # F-08：reconciliation 进入同一 trace（真实受控入口的调用事实）
            self._reconciliation_events.append(_TraceEvent(
                ts=started, identity=f"reconciliation:{self._reconciled_count}",
                identity_kind="reconciliation_id", outcome="requested",
                reason_code="RECONCILIATION_REQUIRED",
                detail="controlled reconciliation entry point invoked"))
            log_event("execution", "reconciliation_requested", runtime_id=self._identity.runtime_id,
                      run_id=self._session.run_id, mode=self.profile.mode.value,
                      reason_code="RECONCILIATION_REQUIRED", count=self._reconciled_count)
            return {"requested": True, "count": self._reconciled_count}

        # F-05：真实边界的延迟观测（旁路；不改交易语义）
        from runtime.latency_observer import ExecutionLatencyObserver

        self._latency_observer = ExecutionLatencyObserver(log=self._latency_log, clock=self.profile.clock)
        # PAPER/REPLAY 的执行链路（既有组件）：用于真实 smoke 场景产生真实 ack 延迟样本
        self._execution = None
        if self.profile.mode in (RuntimeMode.REPLAY, RuntimeMode.PAPER):
            from execution.adapters.paper import PaperBroker
            from execution.engine import ExecutionEngine
            from execution.manager import OrderManager
            from risk.gate import RiskGate
            from risk.limits import RiskLimits

            # P0001.17 §3：本地 Replay/PAPER 的执行适配器。
            #   simulation.enabled=true ⇒ 使用既有 `SimulatedVenue`（P0001.8 事件级模拟成交：队列近似/费率/延迟），
            #   由市场事件驱动真实成交 → FillLedger/Accounting；否则保持既有 PaperBroker（手工注入 fill）。
            local_adapter: object
            self._simulated_venue = None
            if values.get("simulation.enabled") is True:
                from execution.simulation.fees import FeeSchedule
                from execution.simulation.latency import LatencyModel
                from execution.simulation.venue import SimulatedVenue

                submit_ms = values.get("simulation.latency.submit_ms")
                cancel_ms = values.get("simulation.latency.cancel_ms")
                maker_fee = values.get("simulation.fees.maker_fee_rate")
                fee_asset = values.get("simulation.fees.fee_asset")
                if None in (submit_ms, cancel_ms, maker_fee, fee_asset):
                    raise AssemblyError(
                        "simulation.enabled requires explicit simulation.latency.* / simulation.fees.* values "
                        "(no invented defaults)")
                self._simulated_venue = SimulatedVenue(
                    symbol=self.profile.symbol,
                    latency=LatencyModel(submit_latency_ms=int(submit_ms), cancel_latency_ms=int(cancel_ms)),
                    fee_schedule=FeeSchedule(maker_fee_rate=float(maker_fee), fee_asset=str(fee_asset)),
                    venue=Venue(self.profile.venue.lower()))
                local_adapter = self._simulated_venue
            else:
                local_adapter = PaperBroker()
            # P0001.15 §9：本地执行适配器由统一 connector 持有（engine 只依赖 connector seam）
            self._execution_connector = PaperExecutionConnector(
                broker=local_adapter, venue_identity=self._resolve_venue_identity(), symbol=self.profile.symbol,
                clock=self.profile.clock)
            self._execution_connector.connect()      # P0001.15 §9：paper 执行 connector 在本进程内就绪
            manager = OrderManager(tracker=self._tracker_owner, adapter=self._execution_connector)
            # F-08：执行边界归一化**只在显式配置舍入模式时**启用（不发明业务值）
            normalizer = None
            price_rounding = values.get("venue.normalization.price_rounding")
            quantity_rounding = values.get("venue.normalization.quantity_rounding")
            if (self._trading_rules is not None and isinstance(price_rounding, str)
                    and isinstance(quantity_rounding, str)):
                from decimal import Decimal as _Decimal

                from execution.normalization import OrderNormalizer

                normalizer = OrderNormalizer(
                    tick_size=_Decimal(str(values["venue.rules.tick_size"])),
                    step_size=_Decimal(str(values["venue.rules.step_size"])),
                    price_rounding=price_rounding, quantity_rounding=quantity_rounding)
            limit_values: dict[str, float] = {}
            for key, field in (("risk.max_position_qty", "max_position_qty"),
                               ("risk.max_open_order_exposure", "max_open_order_exposure"),
                               ("risk.max_position_notional", "max_position_notional"),
                               ("risk.max_daily_loss", "max_daily_loss"),
                               ("risk.max_drawdown_pct", "max_drawdown_pct")):
                if key in values:
                    limit_values[field] = float(values[key])
            self._risk_limits = RiskLimits(**limit_values)                    # type: ignore[arg-type]
            self._execution = ExecutionEngine(
                accounting=(accounting if accounting is not None else __import__(
                    "portfolio.accounting", fromlist=["AccountingCore"]).AccountingCore(initial_balance=0.0)),
                # 只启用显式配置的限额：未配置的限额不强制对应事实存在（不是 bypass；配置了的仍严格判定）
                gate=RiskGate(self._risk_limits),
                manager=manager, book_healthy=True,
                normalizer=normalizer,
                latency_observer=lambda kind, ts: self._latency_note(kind, ts))
        # P0001.14：MakerPolicy / PredictionRuntime 均由显式配置构造（缺键 ⇒ None；不发明业务数值）
        _venue = self._resolve_venue_identity()
        _registry = self._resolve_instrument_registry()
        self._maker_policy = build_maker_policy(
            values, instrument_id=("" if _registry is None else _registry.current.instrument_id),
            venue_id=_venue.venue_id)
        self._prediction_runtime = self._build_prediction_runtime(values)

        self._safety_projection = ExecutionSafetyProjection(
            clock=self.profile.clock, policy=self._safety_policy,
            rules_provider=lambda: self._trading_rules,
            limit_definition_provider=lambda: self._limit_definition,
            usage_provider=self._usage_snapshot,
            latency_log=self._latency_log,
            private_latency_provider=self._private_latency_state,
            rate_facts_provider=self._usage_snapshot,
            exposure_provider=self._exposure,
            reconciliation_provider=self._reconciliation_state,
            reconciliation_requester=_request_reconciliation)
        return ProductService(
            identity=self._identity,
            config_snapshot=lambda: self._cfg,
            run_registry=lambda: self._registry,
            runtime_status=lambda: self._tracker.status(),
            execution_safety=lambda: self._safety_projection,
            tracker=lambda: self._tracker_owner,
            account_timeline=lambda: self._account_timeline,
            accounting_health=self._accounting_health,
            action_gateway=lambda: self._gateway,
            assistant=lambda: self._assistant,
            # 有真实 feed 时接入市场历史缓冲；否则保持 UNKNOWN（不伪造 healthy / 0）
            market_state=lambda: (self._feed_provider.last_state if self._feed_provider else None),
            market_history=lambda: self._history,
            projection_config=lambda: self._projection_config,
            # P0001.15 §21–§26：instrument / venue / reference price / connector health（两个 connector 分开）
            instrument=self._instrument_view,
            instrument_registry=lambda: self._resolve_instrument_registry(),
            venue=self._resolve_venue_identity,
            reference_price=self._reference_price_for_risk,
            market_connector_health=self._market_connector_health,
            private_connector_health=self._execution_connector_health,
            orders_for_decision=self._orders_for_decision,
            # P0001.14：决策链真实 facts（loop 未运行时 ⇒ None，产品层如实 UNKNOWN/ABSENT）
            prediction=lambda: (self._decision_loop.latest_prediction
                                if self._decision_loop is not None else None),
            maker_decision=lambda: (self._decision_loop.latest_decision
                                    if self._decision_loop is not None else None),
            risk_snapshot=self._risk_snapshot_provider,
            risk_limits=lambda: self._risk_limits,
            prediction_fresh=self._prediction_fresh,
            risk_rejects=self._risk_reject_codes,
            accounting=lambda: self._accounting,
            accounting_facts=(lambda: (self._accounting_provider.facts()
                                       if self._accounting_provider is not None else None)),
            prediction_provider_status=self._prediction_provider_status,
            # F-08：真实边界事实 → trace（缺则产品层如实 ABSENT）
            risk_decisions=self._risk_decision_views,
            normalization_evidence=self._normalization_views,
            reconciliation_events=lambda: tuple(self._reconciliation_events),
            ack_latency=lambda: self._latency_log,
            execution_events=self._execution_event_views,
            fills=self._fill_views,
            recent_fill_limit=(20 if self._accounting is not None else 0),
            market_state_hash=self._market_identity,
            ops=self.ops_posture,
            run_summary=lambda: self._durable_run_summary(None),
            durable_run_summary=self._durable_run_summary,
            raw_fact_lookup=self._raw_fact_lookup,
            readiness=lambda: (self._decision_loop.readiness_result
                               if self._decision_loop is not None else None),
            health=lambda: {"notes": (f"mode={self.profile.mode.value}",)},
            clock=self.profile.clock,
        )

    def _register_baseline_handlers(self) -> None:
        """基线 handler：只读 + 产品态 + 无副作用的回放/报告（不注册任何写能力）。"""
        gateway = self._gateway

        def snapshot_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            snap = self._service.snapshot()
            return HandlerResult(result={"runtime_id": snap.runtime.runtime_id,
                                         "runtime_state": self._tracker.status().state.value,
                                         "blockers": len(snap.blockers)},
                                 fact_refs=("snapshot:current",))

        def blockers_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            snap = self._service.snapshot()
            return HandlerResult(result={"blockers": [f"{b.owner.value}:{b.reason_code}" for b in snap.blockers]})

        def health_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            snap = self._service.snapshot()
            return HandlerResult(result={"runtime_state": self._tracker.status().state.value,
                                         "market_healthy": snap.market.healthy.known and snap.market.healthy.value,
                                         "health": {k: (v.value if v.known else "UNKNOWN")
                                                    for k, v in (("prediction_provider", snap.health.prediction_provider),
                                                                 ("accounting", snap.health.accounting))}})

        def raw_facts_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            kind = str(request.parameters.get("kind") or "")
            identity = str(request.parameters.get("identity") or "")
            view = self._service.raw_facts_view(kind, identity)
            return HandlerResult(result={"available": bool(view.available),
                                         "fields": [name for name, _ in view.facts]})

        def compare_runs_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            left = str(request.parameters.get("left") or "")
            right = str(request.parameters.get("right") or "")
            if not left or not right:
                raise ValueError("compare.runs requires left and right run ids")
            comparison = self._registry.compare(left, right)
            return HandlerResult(result={"left": comparison.left_run_id, "right": comparison.right_run_id,
                                         "metrics": len(comparison.metrics)},
                                 fact_refs=(f"run:{left}", f"run:{right}"))

        def report_generate_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            """复用既有 RunSummary / report builder（不建立第二套 report owner）。"""
            summary = build_run_summary(identity=self._identity, run_id=self._session.run_id,
                                        started_at=self._identity.started_at,
                                        ended_at=int(self.profile.clock()))
            payload = json.loads(summary_to_json(summary))
            return HandlerResult(result={"format": str(request.parameters.get("format") or "json"),
                                         "run_id": self._session.run_id,
                                         "metrics": sorted(payload.get("metrics", {}).keys())},
                                 fact_refs=(f"run:{self._session.run_id}",))

        def explain_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            kind = str(request.parameters.get("kind") or "decision")
            identity = str(request.parameters.get("identity") or context.selected.get("order") or "")
            if not identity:
                raise ValueError("explain.entity requires an identity")
            # F-12/F-15：`kind=ops` 解释 operational posture；其余走既有 entity explain
            explanation = (self._assistant.explain_ops(identity) if kind == "ops"
                           else self._assistant.explain(kind, identity))
            return HandlerResult(result=explanation, fact_refs=(f"explain:{kind}:{identity}",))

        def navigate_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            # F-16：与 UI 共用 product.navigation 契约
            from product.navigation import NavigationError, surface_route

            surface = str(request.parameters.get("surface") or "monitor")
            detail = request.parameters.get("detail")
            identity = request.parameters.get("identity")
            try:
                target = surface_route(surface, detail=(str(detail) if detail else None),
                                       identity=(str(identity) if identity else None))
            except NavigationError as exc:
                raise ValueError(str(exc)) from None
            return HandlerResult(result=target.to_payload())

        def select_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            from product.navigation import NavigationError, entity_route

            kind = str(request.parameters.get("kind") or "order")
            identity = str(request.parameters.get("identity") or "")
            if not identity:
                raise ValueError("select.entity requires an identity")
            try:
                target = entity_route(kind, identity)
            except NavigationError as exc:
                raise ValueError(str(exc)) from None
            return HandlerResult(result={"selected": {kind: identity},
                                         "navigation": target.to_payload()})

        def view_configure_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            return HandlerResult(result={"bounds": {k: request.parameters.get(k)
                                                    for k in ("window_ms", "bucket_ms", "max_points")}})

        for action_id, handler in (
            ("inspect.snapshot", snapshot_handler), ("query.blockers", blockers_handler),
            ("query.health", health_handler), ("query.raw_facts", raw_facts_handler),
            ("compare.runs", compare_runs_handler), ("report.generate", report_generate_handler),
            ("explain.entity", explain_handler), ("navigate.surface", navigate_handler),
            ("select.entity", select_handler), ("view.configure", view_configure_handler),
        ):
            gateway.register(action_id, handler)
        # 执行安全只读动作：未接线投影时不注册（Manifest 如实显示不可用）
        # replay.control：只有在调用方提供了 replay 控制对象时才注册（见 with_replay_control）

    def with_replay_control(self, control: object) -> "ProductRuntime":
        """显式接入 replay 控制（只在 REPLAY 模式下；由调用方提供既有 ReplayControl）。"""
        if self.profile.mode is not RuntimeMode.REPLAY:
            raise AssemblyError("replay control is only available for REPLAY runtime")
        self._gateway.register("replay.control", lambda request, context: HandlerResult(
            result={"verb": str(request.parameters.get("verb") or "play")},
            fact_refs=("replay:command",)))
        return self

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> RuntimeStatus:
        """建立 run、写 active marker，并把 runtime 标为 RUNNING。"""
        # G-A4 诚实性护栏：产品装配在 TESTNET/LIVE 模式下不构造交易所 connector；
        # 不允许“吃本地 event store 却显示 binance:market CONNECTED” ⇒ 显式拒绝并指向授权路径。
        if self.profile.mode in (RuntimeMode.TESTNET, RuntimeMode.LIVE):
            raise AssemblyError(
                "runtime.assembly does not own a real venue connector for mode "
                f"{self.profile.mode.value}: no live TESTNET/LIVE data or write path is wired here. "
                "Use the dedicated authorised TESTNET stack (runtime.testnet / "
                "tests/acceptance/testnet_acceptance_run.py with explicit confirm flags); "
                "see docs/RUNBOOK.md section 8")
        now = int(self.profile.clock())
        self._tracker.mark_starting(now_ms=now, detail="assembling runtime")
        self._host.start()
        detail = "running" if self.profile.feed is not None else "idle: no market data source configured"
        status = self._tracker.mark_running(now_ms=now, run_id=self._session.run_id, quoting=False,
                                            detail=detail)
        log_event("runtime", "runtime_start", runtime_id=self._identity.runtime_id,
                  run_id=self._session.run_id, mode=self.profile.mode.value,
                  state=status.state.value, bind_host=self.profile.host,
                  quoting=status.quoting)
        if self.profile.feed is not None:
            self._start_feed()
            self._start_decision_loop()
        elif getattr(self, "_market_source", "event-store") == "binance-public":
            self._start_public_market()
        return status

    # ------------------------------------------------------------------ composition（不含 feed 业务逻辑）

    def _start_feed(self) -> None:
        """构造既有 feed provider（市场算法全在既有模块里），并接线到 Product 投影。"""
        profile = self.profile.feed
        assert profile is not None  # noqa: S101
        from product.market_projection import MarketProjectionConfig
        from runtime.provider import FeedConfig, MarketFeedProvider

        provider = MarketFeedProvider(
            config=FeedConfig(event_store=profile.event_store,
                              projection=MarketProjectionConfig(window_ms=profile.window_ms,
                                                                bucket_ms=profile.bucket_ms,
                                                                max_points=profile.max_points,
                                                                price_levels=profile.price_levels),
                              history_capacity=profile.history_capacity,
                              view_depth=profile.view_depth,
                              events_per_second=self._feed_events_per_second()),
            venue=Venue(self.profile.venue.lower()),
            symbol=self.profile.symbol, mode=self.profile.mode, run_id=self._session.run_id,
            clock=self.profile.clock)
        self._feed_provider = provider
        self._history = provider.history
        # P0001.15 §11 / 人类裁决 1A：正式 MARK_PRICE 事件驱动 reference price（只接受 MARK_PRICE）
        reference_prices = self._resolve_reference_prices()
        provider.reference_price_sink = reference_prices.observe_market_event
        if self._simulated_venue is not None:
            provider.market_event_sink = self._simulated_venue.on_market_event
        provider.trade_sink = self._persist_trade
        venue = self._resolve_venue_identity()
        registry = self._resolve_instrument_registry()
        instrument = None if registry is None else registry.current
        self._market_connector = PaperMarketDataConnector(
            venue_identity=venue, symbol=self.profile.symbol, clock=self.profile.clock,
            data_source=f"event_store:{profile.event_store}",
            state_provider=lambda: provider.last_state,
            rules_provider=lambda: self._trading_rules,
            last_event_provider=lambda: self._feed_last_event_timestamps(provider),
            reference_source=(None if instrument is None
                              else reference_prices.sources.get((instrument.instrument_id, PriceType.MARK))))
        self._market_connector.connect()
        self._projection_config = provider.config.projection
        self._service.market_state = lambda: provider.last_state   # 只读视图（provider 拥有事实）
        self._service.market_history = lambda: provider.history
        self._service.projection_config = lambda: provider.config.projection
        # Step 4：账户采样在 feed provider 构造之后接线（复用同一 accounting 事实）
        provider.on_data_timestamp = self.note_data_timestamp                 # 运行期推进（真实事件驱动）
        if self._accounting_provider is not None and self._account_timeline is not None:
            provider.account_provider = self._accounting_provider.sample   # type: ignore[attr-defined]
            provider.history_account = self._account_timeline              # type: ignore[attr-defined]
        provider.start()

    def _start_public_market(self) -> None:
        """把产品入口接到公开行情（默认 Binance 公开源；经统一 `MarketSourceAdapter`，只读）。

        扩展点：任何实现 `venue.market_source.MarketSourceAdapter` 的源都可通过注入缝
        `runtime.market_source_adapter` 接入（免费/付费源），无需修改核心所有权。
        """
        from product.market_projection import BoundedMarketHistory
        from runtime.public_market import PublicMarketPump

        values = self._config_values()
        history_capacity = int(values.get("projection.history_capacity", 6_000) or 6_000)
        injected = getattr(self, "market_source_adapter", None)
        if injected is not None:
            adapter = injected
            connector = getattr(self, "market_data_connector", None)
        else:
            adapter, connector = self._build_binance_public_source(
                history_capacity=history_capacity, values=values)
        history = BoundedMarketHistory(capacity=history_capacity, run_id=self._session.run_id)

        def _on_mark(price: float, received_at: int) -> None:
            accounting = self._accounting
            if accounting is not None:
                accounting.update_mark_price(self.profile.symbol, price, timestamp=received_at)

        def _on_state(state: object) -> None:
            # 统一推进 data timestamp（取自 MarketState 的 exchange 时间；真实事实驱动）
            try:
                ts = int(state.time.as_of_exchange_ts)
                self.note_data_timestamp(ts)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._persist_market_point()
            except Exception:  # noqa: BLE001
                pass

        pump = PublicMarketPump(
            adapter, history=history, clock=self.profile.clock, connector=connector,
            pump_timeout_s=0.5, max_states=256, on_mark=_on_mark, on_state=_on_state)
        # 默认路径的 connector 同时负责 runtime.connect + 置 connected；自定义源只有 adapter
        if connector is not None:
            connector.connect()
        else:
            adapter.connect()
        # 先把 history 接到 self（on_state 会立刻在 pump 线程上触发 persist，不能晚于 start）
        self._public_pump = pump
        self._market_connector = connector
        self._history = history
        pump.start()
        self._service.market_state = lambda: (history.states()[-1] if history.states() else None)
        self._service.market_history = lambda: history
        log_event("runtime", "public_market_start", runtime_id=self._identity.runtime_id,
                  run_id=self._session.run_id, source=adapter.source_id)

    def _build_binance_public_source(self, *, history_capacity: int,
                                      values: dict[str, object]) -> tuple[object, object]:
        """默认免费源：Binance 公开 REST/WS（无需凭据）经适配器接入。"""
        from connectors.binance.market_data.endpoints import REST_BASE_URL, WS_HOST
        from connectors.binance.market_data.runtime import (
            LiveMarketDataConfig, LiveMarketDataRuntime)
        from connectors.binance.market_data.transport import (
            ReconnectPolicy, connect as _real_connect)
        from connectors.binance.market_data.snapshot import UrllibJsonClient
        from connectors.binance.market_connector import BinanceMarketDataConnector
        from connectors.binance.public_source import BinancePublicSourceAdapter
        from domain.instruments import instrument_id_for
        from venue.identity import binance_venue

        rest_base = str(values.get("market.public_rest_base", REST_BASE_URL))
        ws_host = str(values.get("market.public_ws_host", WS_HOST))
        transport_factory = getattr(self, "_public_transport_factory", None) or _real_connect
        http_client = getattr(self, "_public_http_client", None) or UrllibJsonClient(base_url=rest_base)
        venue_identity = binance_venue(environment=VenueEnvironment.LIVE)
        runtime = LiveMarketDataRuntime(
            config=LiveMarketDataConfig(
                symbol=self.profile.symbol, depth_speed="100ms", mark_price_speed="1s", depth_limit=100,
                connect_timeout_s=10.0, read_timeout_s=0.5, snapshot_timeout_s=10.0,
                exchange_info_timeout_s=10.0,
                reconnect=ReconnectPolicy(max_attempts=5, base_backoff_ms=500, max_backoff_ms=5_000),
                resync_cooldown_ms=1_000, history_limit=history_capacity, ws_host=ws_host,
                max_book_age_ms=int(values.get("market.max_book_age_ms", 5_000) or 5_000)),
            http_client=http_client,
            transport_factory=lambda url, timeout_s: transport_factory(url, timeout_s=timeout_s),
            clock=self.profile.clock)
        connector = BinanceMarketDataConnector(
            runtime=runtime, venue_identity=venue_identity,
            instrument_id=instrument_id_for(self.profile.symbol, venue_id=venue_identity.venue_id),
            clock=self.profile.clock)
        adapter = BinancePublicSourceAdapter(runtime)
        runtime.load_trading_rules()
        self._trading_rules = runtime.trading_rules
        return adapter, connector

    def _raw_fact_lookup(self, kind: str, identity: str) -> object | None:
        """F5：把已批准的 raw-fact kind 接到**既有 Owner**（canonical identity 直接查找）。

        - order          ⇒ OrderTracker（含终态订单）
        - fill           ⇒ 既有 FillLedger 视图（按 client_order_id / trade_id）
        - decision       ⇒ 既有 maker_decision provider
        - execution_event ⇒ 既有订单生命周期事实（按 client_order_id）
        - prediction     ⇒ 既有 prediction record provider（按 request_id）
        找不到 ⇒ None（API ⇒ 404）；**不新建第二套事实存储**。
        """
        if kind == "order":
            tracker = self._tracker_owner
            return tracker.order(identity) if tracker is not None else None
        if kind == "fill":
            for fill in self._fill_views():
                if identity in (getattr(fill, "client_order_id", None),
                                getattr(fill, "trade_id", None)):
                    return fill
            return None
        if kind == "decision":
            return self._service.maker_decision()
        if kind == "execution_event":
            for event in self._execution_event_views():
                if getattr(event, "client_order_id", None) == identity:
                    return event
            return None
        if kind == "prediction":
            record = self._service.prediction()
            if record is not None and str(getattr(record, "request_id", "")) == identity:
                return record
            return None
        return None

    # ---------------------------------------------------------------- P0001.14 decision loop

    def _risk_snapshot_provider(self) -> object | None:
        execution = getattr(self, "_execution", None)
        if execution is None:
            return None
        self._apply_reference_price()          # P0001.15：正式 MARK → accounting（仅 known）
        try:
            return execution.snapshot(self.profile.symbol, now_ms=int(self.profile.clock()))
        except Exception:  # noqa: BLE001 - 读不到就是 UNKNOWN
            return None

    def _prediction_fresh(self) -> bool | None:
        loop = getattr(self, "_decision_loop", None)
        runtime = getattr(self, "_prediction_runtime", None)
        record = loop.latest_prediction if loop is not None else None
        if record is None or runtime is None:
            return None
        try:
            return not bool(runtime.is_expired(record))
        except Exception:  # noqa: BLE001
            return None

    def _risk_reject_codes(self) -> tuple[str, ...]:
        execution = getattr(self, "_execution", None)
        if execution is None:
            return ()
        return tuple(str(getattr(getattr(item, "reason_code", None), "value", ""))
                     for item in execution.rejections)

    # ---------------------------------------------------------------- P0001.15 identity / reference price

    def _resolve_venue_identity(self) -> object:
        """venue identity：REPLAY/PAPER ⇒ paper venue；TESTNET/LIVE ⇒ binance venue（本阶段不启用写路径）。"""
        if self._venue_identity is None:
            self._venue_identity = venue_for_mode(self.profile.mode)
        return self._venue_identity

    def _resolve_instrument_registry(self) -> object:
        """由显式配置的 venue rules 构造 InstrumentSpec（数值来自 config，不在此处发明）。"""
        if self._instrument_registry is not None:
            return self._instrument_registry
        values = self._config_values()

        def number(key: str) -> float | None:
            value = values.get(key)
            return None if value is None else float(value)

        tick = number("venue.rules.tick_size")
        step = number("venue.rules.step_size")
        min_qty = number("venue.rules.min_qty")
        min_notional = number("venue.rules.min_notional")
        if None in (tick, step, min_qty, min_notional):
            # 缺 venue rules ⇒ 不伪造 instrument 参数（保持 UNKNOWN，不构造 spec）
            return None
        venue = self._resolve_venue_identity()
        base, quote = _split_symbol(self.profile.symbol)
        spec = perpetual_crypto_spec(
            instrument_id=instrument_id_for(self.profile.symbol, venue_id=venue.venue_id),
            symbol=self.profile.symbol, base_asset=base, quote_asset=quote, settlement_asset=quote,
            price_tick=tick, quantity_step=step, min_quantity=min_qty, min_notional=min_notional)
        self._instrument_registry = InstrumentRegistry(
            instruments=(spec,), current_id=spec.instrument_id)
        return self._instrument_registry

    def _resolve_reference_prices(self) -> object:
        """正式 reference price provider + MARK source（由 `MarketEvent.MARK_PRICE` 驱动）。"""
        if self._reference_prices is not None:
            return self._reference_prices
        from domain.instruments import PriceType

        registry = self._resolve_instrument_registry()
        venue = self._resolve_venue_identity()
        provider = ReferencePriceProvider(venue_identity=venue)
        if registry is not None:
            instrument = registry.current
            source = MarkPriceReferenceSource(venue_identity=venue, instrument_id=instrument.instrument_id)
            provider.register(instrument.instrument_id, PriceType.MARK, source)
        self._reference_prices = provider
        return provider

    def _reference_price_for_risk(self) -> object | None:
        registry = self._resolve_instrument_registry()
        if registry is None:
            return None
        provider = self._resolve_reference_prices()
        instrument = registry.current
        return provider.for_risk(instrument, now_ms=int(self.profile.clock()))

    def _apply_reference_price(self) -> None:
        """把**正式 MARK**（且仅 MARK）推到既有 accounting owner；未知 ⇒ 什么也不推（fail closed）。

        P0001.15 §12 / 人类裁决 1：不使用 last trade / mid 替代；`max_mark_age_ms` 仍由 Risk 既有规则判定。
        """
        accounting = self._engine_accounting()
        if accounting is None or self._execution is None:
            return
        reference = self._reference_price_for_risk()
        if reference is None or not getattr(reference, "known", False):
            return
        try:
            accounting.update_mark_price(self.profile.symbol, float(reference.price),
                                         timestamp=int(reference.as_of))
        except Exception as exc:  # noqa: BLE001 - 注入失败不得打断决策（保持 UNKNOWN）
            log_event("market", "reference_price_injection_failed", level=30,
                      runtime_id=self._identity.runtime_id, error=type(exc).__name__)

    def _engine_accounting(self) -> object | None:
        execution = getattr(self, "_execution", None)
        return None if execution is None else getattr(execution, "accounting", None)

    @staticmethod
    def _feed_last_event_timestamps(provider: object) -> tuple[int, int] | None:
        """feed 已消费的最后一个事件的 (exchange_ts, process_ts)；未观测 ⇒ None（UNKNOWN）。"""
        stats = getattr(provider, "stats", {}) or {}
        exchange_ts = stats.get("last_ts")
        process_ts = stats.get("last_process_ts", exchange_ts)
        if not isinstance(exchange_ts, int) or not isinstance(process_ts, int):
            return None
        return exchange_ts, process_ts

    def _feed_events_per_second(self) -> int | None:
        """回放节流（显式配置才生效；未配置 ⇒ 不限速，保持既有行为）。"""
        value = self._config_values().get("feed.events_per_second")
        if value is None:
            return None
        return int(value)

    def _market_connector_health(self) -> object | None:
        connector = getattr(self, "_market_connector", None)
        if connector is None:
            return None
        return connector.health(now_ms=int(self.profile.clock()))

    def _execution_connector_health(self) -> object | None:
        connector = getattr(self, "_execution_connector", None)
        if connector is None:
            return None
        return connector.health(now_ms=int(self.profile.clock()))

    def _instrument_view(self) -> object | None:
        registry = self._resolve_instrument_registry()
        return None if registry is None else registry.current

    def _note_decision_overlay(self, decision: object | None) -> None:
        """决策 → 展示缓冲（只读叠加；字段搬运，不推测状态）。"""
        if decision is None or self._history is None:
            return
        bid = getattr(decision, "bid", None)
        ask = getattr(decision, "ask", None)
        side = "buy" if getattr(bid, "action", None) is not None else "sell"
        chosen = bid if getattr(bid, "action", None) is not None else ask
        self._persist_fact("decision", {"ts": int(getattr(decision, "at_ms", 0)),
                                       "decision_id": getattr(decision, "decision_id", None),
                                       "mode": str(getattr(getattr(decision, "mode", None), "value", "") or "")})
        self._history.feed_decision(SimpleNamespace(
            ts=int(getattr(decision, "at_ms", 0)),
            side=side,
            action=getattr(chosen, "action", None),
            price=getattr(chosen, "price", None),
            quantity=getattr(chosen, "quantity", None),
            decision_id=getattr(decision, "decision_id", None),
            reason=str(getattr(getattr(decision, "blocked_by", None), "value", "") or "")
            or str(getattr(decision, "detail", "") or ""),
        ))

    def _note_execution_overlay(self, update: object) -> None:
        """订单/成交事实 → 展示缓冲（K 线上的 order/fill 标记）。字段搬运，不推测状态。"""
        if self._history is None or update is None:
            return
        order = getattr(update, "order", None)
        if order is None:
            return
        status = getattr(order, "status", None)
        self._persist_fact("order", {"ts": int(getattr(order, "updated_at", 0)),
                                     "client_order_id": str(getattr(order, "client_order_id", "")),
                                     "status": str(getattr(status, "value", status) or ""),
                                     "venue_order_id": getattr(order, "exchange_order_id", None)})
        self._history.feed_execution(SimpleNamespace(
            ts=int(getattr(order, "updated_at", 0)),
            client_order_id=str(getattr(order, "client_order_id", "")),
            event=str(getattr(status, "value", status) or type(update).__name__),
            detail=str(getattr(update, "detail", "") or "")))

    def _orders_for_decision(self, decision_id: str) -> tuple[object, ...]:
        execution = getattr(self, "_execution", None)
        if execution is None:
            return ()
        return tuple(execution.orders_for_decision(decision_id))

    def _kill_switch(self) -> object:
        """kill switch 由 Risk limits 拥有（本层只读取）。"""
        from risk.types import KillSwitchMode

        limits = getattr(self, "_risk_limits", None)
        if limits is None:
            return KillSwitchMode.NORMAL
        return limits.effective_kill_switch_mode

    def _risk_budget(self, snapshot: object) -> float | None:
        """只消费 Risk domain 的 canonical budget（本层不做风险数学）。"""
        limits = getattr(self, "_risk_limits", None)
        if limits is None:
            return None
        try:
            return remaining_exposure_budget(snapshot, limits).remaining_notional  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - 算不出就是未知（fail closed）
            return None

    def _build_prediction_runtime(self, values: dict[str, object]) -> object | None:
        """按显式配置构造 `PredictionRuntime`；未配置/不可用 ⇒ None（诚实 UNAVAILABLE）。

        P0001.17：`prediction.provider = "local_trial"` ⇒ 使用**授权**的 `LOCAL_TRIAL` 确定性 provider，
        仅在 REPLAY/PAPER 允许；TESTNET/LIVE 显式拒绝（不静默回退到其它 provider）。
        """
        provider_name = values.get("prediction.provider")
        if provider_name == "local_trial":
            if self.profile.mode not in (RuntimeMode.REPLAY, RuntimeMode.PAPER):
                raise AssemblyError(
                    "prediction.provider=local_trial is a LOCAL TRIAL provider and is refused for "
                    f"{self.profile.mode.value} (TESTNET/LIVE must use a real provider)")
            timeout_ms = values.get("prediction.timeout_ms")
            ttl_ms = values.get("prediction.ttl_ms")
            if timeout_ms is None or ttl_ms is None:
                raise AssemblyError(
                    "prediction.provider=local_trial requires explicit prediction.timeout_ms / ttl_ms")
            from prediction.providers.local_trial import LocalTrialProvider
            from prediction.runtime import PredictionRuntime

            return PredictionRuntime(provider=LocalTrialProvider(), clock=_CallableClock(self.profile.clock),
                                     timeout_ms=int(timeout_ms), ttl_ms=int(ttl_ms))
        if provider_name != "systemone":
            return None
        threshold = values.get("prediction.adverse_selection_threshold_bps")
        timeout_ms = values.get("prediction.timeout_ms")
        ttl_ms = values.get("prediction.ttl_ms")
        if threshold is None or timeout_ms is None or ttl_ms is None:
            return None
        if not os.environ.get("OPENROUTER_API_KEY"):
            return None
        try:
            from prediction.providers.systemone import SystemOneProvider, SystemOneTransport
            from prediction.runtime import PredictionRuntime

            provider = SystemOneProvider(transport=SystemOneTransport(),
                                         adverse_selection_threshold_bps=float(threshold))
            return PredictionRuntime(provider=provider, clock=_CallableClock(self.profile.clock),
                                     timeout_ms=int(timeout_ms), ttl_ms=int(ttl_ms))
        except Exception:  # noqa: BLE001 - credential/阈值不可用 ⇒ 不构造
            return None

    def attach_prediction_provider(self, provider: object, *, timeout_ms: int, ttl_ms: int,
                                   mode: object | None = None) -> "ProductRuntime":
        """显式注入正式 provider 契约的实现（wiring/集成测试用）；未注入 ⇒ prediction UNAVAILABLE。"""
        from prediction.runtime import PredictionMode, PredictionRuntime

        self._prediction_runtime = PredictionRuntime(
            provider=provider, clock=_CallableClock(self.profile.clock),
            timeout_ms=int(timeout_ms), ttl_ms=int(ttl_ms),
            mode=(mode if mode is not None else PredictionMode.LIVE_REQUERY))
        return self

    def _start_decision_loop(self) -> None:
        """在真实 feed 与 execution 就位后启动决策 loop（REPLAY observe-only / PAPER write-enabled）。"""
        if self._decision_loop is not None or self._feed_provider is None or self._execution is None:
            return
        if self.profile.mode not in (RuntimeMode.REPLAY, RuntimeMode.PAPER):
            return
        from runtime.decision_loop import DecisionLoopConfig, RuntimeDecisionLoop

        options = build_loop_options(self._config_values())
        loop = RuntimeDecisionLoop(
            config=DecisionLoopConfig(symbol=self.profile.symbol, mode=self.profile.mode, **options),
            state_provider=lambda: self._feed_provider.last_state,
            engine=self._execution, tracker=self._tracker_owner,
            risk_budget_provider=self._risk_budget,
            clock=self.profile.clock, policy=self._maker_policy,
            prediction_runtime=self._prediction_runtime,
            kill_switch_provider=self._kill_switch,
            pre_snapshot=self._apply_reference_price,
            # P0001.17 §6/§7：真实 decision/execution 事实进入有界展示缓冲（chart/Activity overlays）
            on_decision=self._note_decision_overlay,
            on_execution=self._note_execution_overlay)
        self._decision_loop = loop
        loop.start()
        log_event("runtime", "decision_loop_start", runtime_id=self._identity.runtime_id,
                  run_id=self._current_run_id(), mode=self.profile.mode.value,
                  policy_configured=self._maker_policy is not None,
                  prediction_provider="wired" if self._prediction_runtime is not None else "unavailable")

    def _stop_decision_loop(self) -> None:
        loop = getattr(self, "_decision_loop", None)
        if loop is not None:
            loop.stop()

    def _disconnect_connectors(self) -> None:
        """P0001.15 §16：stop 时按 connector 各自的生命周期断开（不合并状态）。"""
        for name in ("_market_connector", "_execution_connector"):
            connector = getattr(self, name, None)
            if connector is not None:
                try:
                    connector.disconnect()
                except Exception as exc:  # noqa: BLE001 - 断开失败不得掩盖 stop 的结果
                    log_event("runtime", "connector_disconnect_failed", level=30,
                              runtime_id=self._identity.runtime_id, connector=name,
                              error=type(exc).__name__)

    def _market_identity(self) -> object | None:
        """F-08：MarketState 的既有 canonical 指纹（只读；未接线/无状态 ⇒ None）。"""
        provider = getattr(self, "_feed_provider", None)
        state = getattr(provider, "last_state", None) if provider is not None else None
        if state is None:
            return None
        from prediction.schema import market_state_hash

        return market_state_hash(state)

    def _execution_event_views(self) -> tuple[object, ...]:
        """F-08：订单生命周期事实（终态 / LOST）；来自既有 OrderTracker，不新建事件存储。"""
        tracker = self._tracker_owner
        if tracker is None:
            return ()
        views: list[_LifecycleFact] = []
        for order in getattr(tracker, "orders", ()):
            status = getattr(order, "status", None)
            terminal = bool(getattr(status, "is_terminal", False))
            lost = bool(getattr(status, "is_lost", False))
            if not (terminal or lost):
                continue
            name = getattr(status, "value", status)
            views.append(_LifecycleFact(
                client_order_id=str(order.client_order_id), timestamp=int(order.updated_at),
                event_name=f"OrderStatus:{name}",
                reason=("local uncertainty is not a terminal fact; converge via reconciliation"
                        if lost else "")))
        return tuple(views)

    def _fill_views(self) -> tuple[object, ...]:
        """F-08：成交事实（来自既有 AccountingCore FillLedger；`order_id` = client_order_id）。"""
        accounting = getattr(self, "_accounting", None)
        ledger = getattr(accounting, "fills", None) if accounting is not None else None
        fills = getattr(ledger, "fills", None) if ledger is not None else None
        if not fills:
            return ()
        return tuple(_FillFact(client_order_id=str(fill.order_id), ts=int(fill.exchange_ts),
                               price=float(fill.price), quantity=float(fill.quantity),
                               fee=float(fill.fee), trade_id=str(fill.trade_id)) for fill in fills)

    def _risk_decision_views(self) -> tuple[object, ...]:
        """F-08：engine 的风险判定观测（allow + reject）；未接线 ⇒ 空元组（如实 ABSENT）。"""
        execution = getattr(self, "_execution", None)
        log = getattr(execution, "decision_log", None) if execution is not None else None
        return tuple(log) if log is not None else ()

    def _normalization_views(self) -> tuple[object, ...]:
        """F-08：执行边界归一化证据；未配置 normalizer ⇒ 空元组（如实 ABSENT，不伪造）。"""
        execution = getattr(self, "_execution", None)
        log = getattr(execution, "normalization_log", None) if execution is not None else None
        evidence = getattr(log, "evidence", None) if log is not None else None
        return tuple(evidence()) if callable(evidence) else ()

    def note_data_timestamp(self, ts_ms: int) -> None:
        """运行时推进 data timestamp（由真实 market/runtime event 驱动；不等 stop）。

        P0001.17：本地 Replay/PAPER 的 runtime 时钟同步推进到数据时间（单一时间轴）。
        """
        self._local_data_clock["ms"] = int(ts_ms) if hasattr(self, "_local_data_clock") else None
        self._persist_market_point()
        self._identity = dataclasses_replace(self._identity, data_timestamp=Fact.of(int(ts_ms)))
        service = getattr(self, "_service", None)
        if service is not None:
            service.identity = self._identity          # 让快照立即反映新时间事实

    def _persist_market_point(self) -> None:
        """把当前市场状态按 bucket 节流写入该 run 的持久化时间线（best-effort，不打断运行）。"""
        provider = self._feed_provider
        state = provider.last_state if provider is not None else None
        if state is None and self._history is not None:
            hstates = self._history.states() if callable(getattr(self._history, "states", None)) else ()
            state = hstates[-1] if hstates else None
        if state is None:
            return
        try:
            ts = int(state.time.as_of_exchange_ts)
        except Exception:  # noqa: BLE001 - 状态结构异常 ⇒ 不持久化（不伪造）
            return
        bucket = int(self._projection_config.bucket_ms) if self._projection_config is not None else 1_000
        if self._persisted_market_ts is not None and ts - self._persisted_market_ts < bucket:
            return
        if self._durable_absence_reason() is not None:
            return
        point: dict[str, object] = {"ts": ts}
        price = getattr(state, "price", None)
        if price is not None:
            point.update({"best_bid": price.best_bid, "best_ask": price.best_ask, "mid": price.mid,
                          "spread": price.spread, "microprice": price.microprice})
        quality = getattr(state, "quality", None)
        if quality is not None:
            book_health = getattr(quality, "book_health", None)
            point.update({"book_health": str(getattr(book_health, "value", book_health) or ""),
                          "tradeable": bool(getattr(quality, "tradeable", False))})
        trade = getattr(state, "trade", None)
        if trade is not None:
            point.update({"vwap": getattr(trade, "vwap", None), "trade_count": getattr(trade, "trade_count", None),
                          "cvd": getattr(trade, "cvd", None)})
        try:
            self._registry.append_run_facts(self._session.run_id, "market", [point])
            self._persisted_market_ts = ts
        except Exception as exc:  # noqa: BLE001 - 持久化失败不得影响交易/回放
            log_event("runtime", "run_fact_persist_failed", level=30,
                      runtime_id=self._identity.runtime_id, run_id=self._session.run_id,
                      kind="market", error=type(exc).__name__)

    def _persist_trade(self, event: object) -> None:
        """把一条真实成交写入该 run 的持久化事实（历史 run 的 K 线因此有成交量与成交均价）。"""
        reason = self._durable_absence_reason()
        if reason is not None or not hasattr(self, "_registry"):
            return
        payload = getattr(event, "payload", None)
        try:
            self._registry.append_run_facts(self._session.run_id, "trades", [{
                "ts": int(getattr(event, "exchange_ts")),
                "price": float(getattr(payload, "price")),
                "quantity": float(getattr(payload, "quantity")),
                "aggressor": str(getattr(getattr(payload, "aggressor", None), "value", "") or "")}])
        except Exception as exc:  # noqa: BLE001 - 持久化失败不得影响交易/回放
            log_event("runtime", "run_fact_persist_failed", level=30,
                      runtime_id=self._identity.runtime_id, run_id=self._session.run_id,
                      kind="trades", error=type(exc).__name__)

    def _durable_absence_reason(self) -> str | None:
        """何时**不**持久化事实（明确原因，避免把非交易 run 也写满磁盘）。"""
        if self.profile.feed is None and self._public_pump is None:
            return "no market source (no recorded market timeline)"
        return None

    def _persist_fact(self, kind: str, entry: dict[str, object]) -> None:
        """decision / order / fill 事实持久化（供历史 run 的 Run Review 定位）。"""
        reason = self._durable_absence_reason()
        if reason is not None or not hasattr(self, "_registry"):
            return
        try:
            self._registry.append_run_facts(self._session.run_id, "facts",
                                            [{"kind": kind, **entry}])
        except Exception as exc:  # noqa: BLE001
            log_event("runtime", "run_fact_persist_failed", level=30,
                      runtime_id=self._identity.runtime_id, run_id=self._session.run_id,
                      kind=kind, error=type(exc).__name__)

    def _stop_feed(self) -> None:
        provider = self._feed_provider
        if provider is not None:
            provider.stop()
            self._feed_stats = provider.stats
            self._feed_error = provider.error
            if provider.error is not None:
                log_event("market", "external_fact_source_failure", level=40,
                          runtime_id=self._identity.runtime_id, run_id=self._session.run_id,
                          mode=self.profile.mode.value, reason_code="FEED_SOURCE_FAILED",
                          error=provider.error)
            if provider.data_timestamp_ms is not None:
                self._identity = dataclasses_replace(
                    self._identity, data_timestamp=Fact.of(provider.data_timestamp_ms))

    def stop(self) -> RuntimeStatus:
        """graceful stop：session 先 finalize（COMPLETED），状态置 STOPPED。"""
        now = int(self.profile.clock())
        self._tracker.mark_stopping(now_ms=now)
        self._stop_decision_loop()          # P0001.14：先停 decision loop（无 background thread 泄漏）
        self._stop_feed()
        if self._public_pump is not None:
            self._public_pump.stop()
            self._public_pump = None
        self._disconnect_connectors()       # P0001.15：两个 connector 各自断开
        if self._session.record.status.value == "RUNNING":
            self._host.finish(facts=self._build_summary_facts())
        status = self._tracker.mark_stopped(now_ms=now, detail="graceful stop completed")
        log_event("runtime", "runtime_stop", runtime_id=self._identity.runtime_id,
                  run_id=self._session.run_id, mode=self.profile.mode.value,
                  state=status.state.value)
        return status

    def fail(self, error: str) -> RuntimeStatus:
        """异常终止：run 记 INCOMPLETE（不伪造 COMPLETED），runtime 置 FAILED。"""
        now = int(self.profile.clock())
        self._stop_decision_loop()
        self._disconnect_connectors()
        if self._session.record.status.value == "RUNNING":
            self._session.stop(status=RunStatus.INCOMPLETE, facts=self._build_summary_facts())
        status = self._tracker.mark_failed(now_ms=now, error=error)
        log_event("runtime", "runtime_failure", level=40, runtime_id=self._identity.runtime_id,
                  run_id=self._session.run_id, mode=self.profile.mode.value,
                  state=status.state.value, reason_code="RUNTIME_FAILED", error=error)
        return status

    # ------------------------------------------------------------------ serving

    def create_server(self) -> object:
        self._server = create_server(self._service, host=self.profile.host, port=self.profile.port,
                                     auth_token=self.auth_token)
        return self._server

    def server_url(self) -> str:
        if self._server is None:
            raise AssemblyError("create_server() must be called before server_url()")
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def serve_forever(self) -> None:
        """在 worker 线程服务 HTTP，主线程等待停止信号（signal handler 可安全 shutdown）。"""
        if self._server is None:
            self.create_server()
        stopped = threading.Event()
        _install_stop_signals(self, self._server, stopped)
        worker = threading.Thread(target=self._server.serve_forever,  # type: ignore[attr-defined]
                                  daemon=True)
        worker.start()
        try:
            while not stopped.wait(0.5):
                if self.status.is_terminal:
                    break
        except KeyboardInterrupt:  # pragma: no cover - 交互式 Ctrl+C 兜底
            pass
        finally:
            if not self.status.is_terminal:
                try:
                    self.stop()
                except Exception as exc:  # noqa: BLE001 - stop 失败必须显式记为 FAILED
                    self.fail(f"{type(exc).__name__}")
            self._server.shutdown()      # type: ignore[attr-defined]
            self._server.server_close()  # type: ignore[attr-defined]
            worker.join(timeout=5)


def pathlib_path(value: str) -> Path:
    return Path(value).expanduser()


def build_profile_from_args(argv: Sequence[str] | None = None) -> RuntimeProfile:
    """从命令行构造 profile（默认 REPLAY + loopback；不提供任何业务数值默认值）。"""
    parser = argparse.ArgumentParser(prog="python3 -m runtime.assembly",
                                     description="Probex product assembly entry point")
    parser.add_argument("--mode", default=RuntimeMode.REPLAY.value.lower(),
                        choices=[mode.value.lower() for mode in ALLOWED_MODES],
                        help="runtime mode (default: replay; testnet/live must be explicit)")
    parser.add_argument("--symbol", required=True, help="traded symbol, e.g. BTCUSDT")
    parser.add_argument("--environment", default="local")
    parser.add_argument("--venue", default="binance")
    parser.add_argument("--host", default=os.environ.get("PROBEX_BIND_HOST", DEFAULT_HOST),
                        help="bind address (default 127.0.0.1; non-loopback requires opt-in + token)")
    parser.add_argument("--port", type=int, default=0, help="0 = ephemeral port")
    parser.add_argument("--allow-non-loopback", action="store_true",
                        default=str(os.environ.get("PROBEX_ALLOW_NON_LOOPBACK", "")).lower() in _TRUE_VALUES,
                        help="explicitly allow binding to a non-loopback address (fail closed otherwise)")
    parser.add_argument("--auth-token-ref",
                        default=os.environ.get("PROBEX_AUTH_TOKEN_REF") or None,
                        help="secret reference for the API bearer token, e.g. env:PROBEX_API_TOKEN")
    parser.add_argument("--run-registry-dir",
                        default=os.environ.get(RUN_REGISTRY_ENV, DEFAULT_RUN_REGISTRY_DIR),
                        help=f"durable run registry dir (env {RUN_REGISTRY_ENV})")
    parser.add_argument("--config", action="append", default=[],
                        help="resolved config entry as name=value (source=CLI); repeatable")
    parser.add_argument("--config-file", default=None,
                        help="JSON file of resolved non-sensitive config values (source=FILE)")
    parser.add_argument("--event-store", default=None,
                        help="event store path; required for replay/paper real runs")
    parser.add_argument("--market-source", default="event-store",
                        choices=("event-store", "binance-public"),
                        help="market source: event-store (default) or binance-public (real continuous public data, read-only, no credentials)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    cli_entries = tuple(ConfigEntry(name=name, source=ConfigSource.CLI, value=Fact.of(value))
                        for item in args.config for name, _, value in [str(item).partition("=")])
    file_entries: tuple[ConfigEntry, ...] = ()
    if args.config_file:
        import pathlib as _pathlib

        file_values = json.loads(_pathlib.Path(args.config_file).read_text(encoding="utf-8"))
        if not isinstance(file_values, dict):
            raise AssemblyError("--config-file must contain a JSON object")
        file_entries = tuple(ConfigEntry(name=str(name), source=ConfigSource.FILE, value=Fact.of(value))
                             for name, value in file_values.items())
    # F-13：ENV 层（真实进程环境；只把实际存在的变量变成候选项）；优先级由 resolver 固定
    env_entries = env_config_entries(os.environ)
    entries = env_entries + file_entries + cli_entries
    if not entries:
        # 显式给出最小可审计输入（不是业务默认值：它只是"本次运行的身份描述"）
        entries = (ConfigEntry(name="symbol", source=ConfigSource.CLI, value=Fact.of(args.symbol)),
                   ConfigEntry(name="mode", source=ConfigSource.CLI,
                               value=Fact.of(str(args.mode).upper())))
    mode = RuntimeMode(str(args.mode).upper())
    # 只消费 resolver 输出（不在装配里自己实现一套优先级）
    resolved = {entry.name: entry.value.value for entry in resolve_config(entries)
                if entry.value.known}
    feed = None
    if args.event_store:
        required = ("projection.window_ms", "projection.bucket_ms", "projection.max_points",
                    "projection.price_levels", "projection.history_capacity", "projection.view_depth")
        missing = [name for name in required if name not in resolved]
        if missing:
            raise AssemblyError(
                "event-store runs require explicit display bounds in the profile/config file: "
                + ", ".join(missing) + " (no technical defaults are invented by the product)"
            )
        feed = FeedProfile(event_store=args.event_store, window_ms=int(resolved["projection.window_ms"]),
                           bucket_ms=int(resolved["projection.bucket_ms"]),
                           max_points=int(resolved["projection.max_points"]),
                           price_levels=int(resolved["projection.price_levels"]),
                           history_capacity=int(resolved["projection.history_capacity"]),
                           view_depth=int(resolved["projection.view_depth"]))
    elif mode in (RuntimeMode.REPLAY, RuntimeMode.PAPER) and args.market_source != "binance-public":
        raise AssemblyError(f"--event-store is required for a real {mode.value.lower()} run")
    market_source = args.market_source
    if market_source == "binance-public":
        if mode not in (RuntimeMode.REPLAY, RuntimeMode.PAPER):
            raise AssemblyError(
                "--market-source binance-public is only allowed with replay/paper (public observation only)")
    return RuntimeProfile(symbol=args.symbol, config_entries=entries, mode=mode,
                          environment=args.environment, venue=args.venue, host=args.host, port=args.port,
                          run_registry_dir=args.run_registry_dir, feed=feed,
                          market_source=market_source,
                          allow_non_loopback=bool(args.allow_non_loopback),
                          auth_token_ref=(str(args.auth_token_ref) if args.auth_token_ref else None))


def _install_stop_signals(runtime: "ProductRuntime", server: object,
                          stopped: "threading.Event") -> None:
    """把 SIGINT/SIGTERM 变成**优雅关闭**：先 stop()（finalize run），再通知主线程退出。

    注意：`socketserver.shutdown()` 必须从**另一个线程**调用（否则与 serve_forever 死锁），
    因此 serve_forever 在 worker 线程运行、signal handler 只负责 stop + 置事件。
    """

    def handler(signum: int, frame: object) -> None:
        try:
            runtime.stop()
        finally:
            stopped.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover - 非主线程/不支持时忽略
            continue


def main(argv: Sequence[str] | None = None) -> int:
    profile = build_profile_from_args(argv)
    # F-15：日志 sink 可选文件 + 显式轮转配置（未配置 size ⇒ 不轮转，姿态如实报告）
    startup_values = {entry.name: entry.value.value
                      for entry in resolve_config(profile.config_entries) if entry.value.known}
    configure_logging(level=os.environ.get("PROBEX_LOG_LEVEL", "INFO"),
                      file_path=os.environ.get("PROBEX_LOG_FILE") or None,
                      file_max_bytes=(int(startup_values["retention.log_max_bytes"])
                                      if "retention.log_max_bytes" in startup_values else None),
                      backup_count=(int(startup_values["retention.log_backup_count"])
                                    if "retention.log_backup_count" in startup_values else None))
    runtime = ProductRuntime(profile=profile)
    status = runtime.start()
    server = runtime.create_server()
    url = runtime.server_url()
    log_event("runtime", "startup", runtime_id=runtime.identity.runtime_id, run_id=runtime.run_id,
              mode=profile.mode.value, symbol=profile.symbol, state=status.state.value,
              bind_host=profile.host, url=url, run_registry_dir=profile.run_registry_dir,
              auth_required=runtime.auth_required)
    print(json.dumps({"event": "startup", "runtime_id": runtime.identity.runtime_id,
                      "mode": profile.mode.value, "symbol": profile.symbol,
                      "run_id": runtime.run_id, "state": status.state.value,
                      "url": url, "run_registry_dir": profile.run_registry_dir}, ensure_ascii=False),
          flush=True)
    runtime.serve_forever()
    log_event("runtime", "shutdown", runtime_id=runtime.identity.runtime_id, run_id=runtime.run_id,
              mode=profile.mode.value, state=runtime.status.state.value)
    print(json.dumps({"event": "shutdown", "run_id": runtime.run_id,
                      "state": runtime.status.state.value}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
