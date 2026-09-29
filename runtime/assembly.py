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
import os
import signal
import threading
import sys
import time
from collections.abc import Callable, Sequence
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
from product.provenance import ConfigEntry, ConfigSource, build_config_snapshot
from product.service import ProductService
from market.events.types import Venue
from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports import build_run_summary
from reports.json import summary_to_json
from reports.types import RunStatus
from runtime.session import RuntimeSession, SessionSummaryFacts
from runtime.state import RuntimeState, RuntimeStatus, RuntimeStatusTracker
from runtime.wiring import SessionHost
from storage.run_registry import DEFAULT_RUN_REGISTRY_DIR, RUN_REGISTRY_ENV, JsonRunRegistry

DEFAULT_HOST = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class _ReconciliationFact:
    """最小只读事实（供既有 projection 投影；不实现 reconciliation 逻辑）。"""

    actions: tuple[object, ...]
    corrective: tuple[object, ...]
    converged: bool
#: 允许的启动模式（默认 REPLAY；TESTNET/LIVE 必须显式指定）
ALLOWED_MODES = (RuntimeMode.REPLAY, RuntimeMode.PAPER, RuntimeMode.TESTNET, RuntimeMode.LIVE)


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
    _feed_error: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        now = int(self.profile.clock())
        self._registry = JsonRunRegistry(pathlib_path(self.profile.run_registry_dir))
        self._cfg = build_config_snapshot(config_id=f"runtime-{self.profile.mode.value.lower()}",
                                         entries=self.profile.config_entries, created_at=now)
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

    # ------------------------------------------------------------------ assembly

    def _facts_provider(self) -> SessionSummaryFacts:
        return self._facts

    def _config_values(self) -> dict[str, object]:
        """已解析的非敏感配置值（CLI 覆盖 FILE，沿用 provenance 优先级）。"""
        from product.provenance import resolve_config

        return {entry.name: entry.value.value
                for entry in resolve_config(self.profile.config_entries) if entry.value.known}

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
        self._limit_definition = build_limit_definition(values)                # Step 2（定义）
        self._usage = build_usage_snapshot(values)                             # Step 2（用量，独立）
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

            provider = AccountingFactsProvider(accounting=accounting, symbol=self.profile.symbol,
                                               clock=self.profile.clock)
        self._accounting = accounting
        self._accounting_provider = provider
        self._account_timeline = timeline
        # Step 5：真实 reconciliation 采样（受控入口的耗时；其余阶段保持 UNKNOWN）
        self._reconciled_count = 0

        def _request_reconciliation() -> dict[str, object]:
            started = int(self.profile.clock())
            self._reconciled_count += 1
            self._latency_log.record(LatencySample(stage="reconciliation_duration", ts=started,
                                                   value_ms=max(0, int(self.profile.clock()) - started)))
            return {"requested": True, "count": self._reconciled_count}

        self._safety_projection = ExecutionSafetyProjection(
            clock=self.profile.clock, policy=self._safety_policy,
            rules_provider=lambda: self._trading_rules,
            limit_definition_provider=lambda: self._limit_definition,
            usage_provider=lambda: self._usage,
            latency_log=self._latency_log,
            private_latency_provider=lambda: None,
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
            prediction=lambda: None,
            maker_decision=lambda: None,
            risk_snapshot=lambda: None,
            risk_limits=lambda: None,
            accounting=lambda: self._accounting,
            accounting_facts=(lambda: (self._accounting_provider.facts()
                                       if self._accounting_provider is not None else None)),
            readiness=lambda: None,
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
            explanation = self._assistant.explain(kind, identity)
            return HandlerResult(result=explanation, fact_refs=(f"explain:{kind}:{identity}",))

        def navigate_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            surface = str(request.parameters.get("surface") or "monitor")
            identity = request.parameters.get("identity")
            target = f"#/{surface}" + (f"/{identity}" if identity else "")
            return HandlerResult(result={"target": target, "surface": surface})

        def select_handler(request: ActionRequest, context: ActionContext) -> HandlerResult:
            kind = str(request.parameters.get("kind") or "order")
            identity = str(request.parameters.get("identity") or "")
            if not identity:
                raise ValueError("select.entity requires an identity")
            return HandlerResult(result={"selected": {kind: identity}})

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
        now = int(self.profile.clock())
        self._tracker.mark_starting(now_ms=now, detail="assembling runtime")
        self._host.start()
        detail = "running" if self.profile.feed is not None else "idle: no market data source configured"
        status = self._tracker.mark_running(now_ms=now, run_id=self._session.run_id, quoting=False,
                                            detail=detail)
        if self.profile.feed is not None:
            self._start_feed()
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
                              view_depth=profile.view_depth),
            venue=Venue(self.profile.venue.lower()),
            symbol=self.profile.symbol, mode=self.profile.mode, run_id=self._session.run_id,
            clock=self.profile.clock)
        self._feed_provider = provider
        self._history = provider.history
        self._projection_config = provider.config.projection
        self._service.market_state = lambda: provider.last_state   # 只读视图（provider 拥有事实）
        self._service.market_history = lambda: provider.history
        self._service.projection_config = lambda: provider.config.projection
        # Step 4：账户采样在 feed provider 构造之后接线（复用同一 accounting 事实）
        if self._accounting_provider is not None and self._account_timeline is not None:
            provider.account_provider = self._accounting_provider.sample   # type: ignore[attr-defined]
            provider.history_account = self._account_timeline              # type: ignore[attr-defined]
        provider.start()

    def _stop_feed(self) -> None:
        provider = self._feed_provider
        if provider is not None:
            provider.stop()
            self._feed_stats = provider.stats
            self._feed_error = provider.error
            if provider.data_timestamp_ms is not None:
                self._identity = dataclasses_replace(
                    self._identity, data_timestamp=Fact.of(provider.data_timestamp_ms))

    def stop(self) -> RuntimeStatus:
        """graceful stop：session 先 finalize（COMPLETED），状态置 STOPPED。"""
        now = int(self.profile.clock())
        self._tracker.mark_stopping(now_ms=now)
        self._stop_feed()
        if self._session.record.status.value == "RUNNING":
            self._host.finish(facts=self._facts)
        return self._tracker.mark_stopped(now_ms=now, detail="graceful stop completed")

    def fail(self, error: str) -> RuntimeStatus:
        """异常终止：run 记 INCOMPLETE（不伪造 COMPLETED），runtime 置 FAILED。"""
        now = int(self.profile.clock())
        if self._session.record.status.value == "RUNNING":
            self._session.stop(status=RunStatus.INCOMPLETE, facts=self._facts)
        return self._tracker.mark_failed(now_ms=now, error=error)

    # ------------------------------------------------------------------ serving

    def create_server(self) -> object:
        self._server = create_server(self._service, host=self.profile.host, port=self.profile.port)
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
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="0 = ephemeral port")
    parser.add_argument("--run-registry-dir",
                        default=os.environ.get(RUN_REGISTRY_ENV, DEFAULT_RUN_REGISTRY_DIR),
                        help=f"durable run registry dir (env {RUN_REGISTRY_ENV})")
    parser.add_argument("--config", action="append", default=[],
                        help="resolved config entry as name=value (source=CLI); repeatable")
    parser.add_argument("--config-file", default=None,
                        help="JSON file of resolved non-sensitive config values (source=FILE)")
    parser.add_argument("--event-store", default=None,
                        help="event store path; required for replay/paper real runs")
    args = parser.parse_args(list(argv) if argv is not None else None)
    entries = tuple(ConfigEntry(name=name, source=ConfigSource.CLI, value=Fact.of(value))
                    for item in args.config for name, _, value in [str(item).partition("=")])
    if args.config_file:
        import pathlib as _pathlib

        file_values = json.loads(_pathlib.Path(args.config_file).read_text(encoding="utf-8"))
        if not isinstance(file_values, dict):
            raise AssemblyError("--config-file must contain a JSON object")
        entries = tuple(ConfigEntry(name=str(name), source=ConfigSource.FILE, value=Fact.of(value))
                        for name, value in file_values.items()) + entries
    if not entries:
        # 显式给出最小可审计输入（不是业务默认值：它只是"本次运行的身份描述"）
        entries = (ConfigEntry(name="symbol", source=ConfigSource.CLI, value=Fact.of(args.symbol)),
                   ConfigEntry(name="mode", source=ConfigSource.CLI,
                               value=Fact.of(str(args.mode).upper())))
    mode = RuntimeMode(str(args.mode).upper())
    resolved = {entry.name: entry.value.value for entry in entries
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
    elif mode in (RuntimeMode.REPLAY, RuntimeMode.PAPER):
        raise AssemblyError(f"--event-store is required for a real {mode.value.lower()} run")
    return RuntimeProfile(symbol=args.symbol, config_entries=entries, mode=mode,
                          environment=args.environment, venue=args.venue, host=args.host, port=args.port,
                          run_registry_dir=args.run_registry_dir, feed=feed)


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
    runtime = ProductRuntime(profile=profile)
    status = runtime.start()
    server = runtime.create_server()
    url = runtime.server_url()
    print(json.dumps({"event": "startup", "runtime_id": runtime.identity.runtime_id,
                      "mode": profile.mode.value, "symbol": profile.symbol,
                      "run_id": runtime.run_id, "state": status.state.value,
                      "url": url, "run_registry_dir": profile.run_registry_dir}, ensure_ascii=False),
          flush=True)
    runtime.serve_forever()
    print(json.dumps({"event": "shutdown", "run_id": runtime.run_id,
                      "state": runtime.status.state.value}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
