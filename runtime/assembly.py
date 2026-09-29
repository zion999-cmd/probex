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
import os
import signal
import threading
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from dataclasses import dataclass, field

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
from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports import build_run_summary
from reports.json import summary_to_json
from reports.types import RunStatus
from runtime.session import RuntimeSession, SessionSummaryFacts
from runtime.state import RuntimeState, RuntimeStatus, RuntimeStatusTracker
from runtime.wiring import SessionHost
from storage.run_registry import DEFAULT_RUN_REGISTRY_DIR, RUN_REGISTRY_ENV, JsonRunRegistry

DEFAULT_HOST = "127.0.0.1"
#: 允许的启动模式（默认 REPLAY；TESTNET/LIVE 必须显式指定）
ALLOWED_MODES = (RuntimeMode.REPLAY, RuntimeMode.PAPER, RuntimeMode.TESTNET, RuntimeMode.LIVE)


class AssemblyError(RuntimeError):
    """装配契约错误（缺必填输入 / 非法组合）。"""


def clock_now_ms() -> int:
    """装配入口允许使用 wall-clock（它本身就是 live 边界）。"""
    return int(time.time() * 1000)


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

    def _build_service(self) -> ProductService:
        """组装 ProductService：只注入真实存在的 provider；没有的一律保持 UNKNOWN。"""
        return ProductService(
            identity=self._identity,
            config_snapshot=lambda: self._cfg,
            run_registry=lambda: self._registry,
            runtime_status=lambda: self._tracker.status(),
            action_gateway=lambda: self._gateway,
            assistant=lambda: self._assistant,
            # 无行情/账户/执行源时保持 UNKNOWN（不伪造 healthy / 0）
            market_state=lambda: None,
            prediction=lambda: None,
            maker_decision=lambda: None,
            risk_snapshot=lambda: None,
            risk_limits=lambda: None,
            tracker=lambda: None,
            accounting=lambda: None,
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
        """建立 run 并把 runtime 标为 RUNNING（quota/quoting 只在有依据时给出）。"""
        now = int(self.profile.clock())
        self._tracker.mark_starting(now_ms=now, detail="assembling runtime")
        self._host.start()
        status = self._tracker.mark_running(
            now_ms=now, run_id=self._session.run_id, quoting=False,
            detail="idle: no market data source configured in this build of the assembly")
        return status

    def stop(self) -> RuntimeStatus:
        """graceful stop：session 先 finalize（COMPLETED），状态置 STOPPED。"""
        now = int(self.profile.clock())
        self._tracker.mark_stopping(now_ms=now)
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
    args = parser.parse_args(list(argv) if argv is not None else None)
    entries = tuple(ConfigEntry(name=name, source=ConfigSource.CLI, value=Fact.of(value))
                    for item in args.config for name, _, value in [str(item).partition("=")])
    if not entries:
        # 显式给出最小可审计输入（不是业务默认值：它只是"本次运行的身份描述"）
        entries = (ConfigEntry(name="symbol", source=ConfigSource.CLI, value=Fact.of(args.symbol)),
                   ConfigEntry(name="mode", source=ConfigSource.CLI,
                               value=Fact.of(str(args.mode).upper())))
    return RuntimeProfile(symbol=args.symbol, config_entries=entries,
                          mode=RuntimeMode(str(args.mode).upper()), environment=args.environment, venue=args.venue,
                          host=args.host, port=args.port, run_registry_dir=args.run_registry_dir)


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
