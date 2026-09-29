"""Runtime Run-Lifecycle Integration（P0001.11.1）。

把已完成的 Run Registry 接到**真实 runtime lifecycle**：

    runtime identity established → create_run() → running → graceful stop → finalize(COMPLETED)
    异常退出（created but no finalization） → 下次检查 → INCOMPLETE

纪律：

- **不改交易路径、不改运行决策**：本模块不 import strategy / risk / execution / connectors，
  只在会话边界登记 run、并在结束时把**已记录事实**打包成 `RunSummary`（不重算账户真相）；
- REPLAY / PAPER / TESTNET / LIVE **共用同一套语义**（差异只在 `RuntimeIdentity.mode`）；
- 不引入 daemon、不做 crash recovery manager、不做多进程 writer（单 writer，P0001.11 裁决 B）；
- config fingerprint 在**建立时**绑定，结束时若与当前快照不一致 ⇒ 抛错（不静默覆盖）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType

from market.events.types import Milliseconds

from product.provenance import ConfigSnapshot
from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports.builder import build_run_summary
from reports.json import summary_to_jsonable
from reports.metrics import EquitySample, RealizedTradeResult, compute_metrics, metrics_payload
from reports.types import RunRecord, RunStatus
from storage.run_registry import JsonRunRegistry


class RuntimeSessionError(RuntimeError):
    """会话生命周期契约错误（重复 start、未 start 就 stop、config 漂移等）。"""


@dataclass(frozen=True, slots=True)
class SessionSummaryFacts:
    """结束时打包的**已记录事实**（全部由 Owner 提供；session 不推导任何账户真相）。"""

    telemetry: tuple[object, ...] = ()
    orders: tuple[object, ...] = ()
    fills: int | None = None
    fees: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    max_confirmed_exposure: float | None = None
    max_total_exposure: float | None = None
    final_position: float | None = None
    equity_samples: tuple[EquitySample, ...] = ()
    realized_trades: tuple[RealizedTradeResult, ...] = ()
    risk_rejects: tuple[str, ...] = ()
    readiness_blocks: tuple[str, ...] = ()
    anomalies: tuple[str, ...] = ()
    data_range: tuple[int, int] | None = None


@dataclass(slots=True)
class RuntimeSession:
    """一次 runtime 会话（≈ 一个进程/一次运行实例）。"""

    mode: RuntimeMode
    environment: str
    venue: str
    symbol: str
    registry: JsonRunRegistry
    clock: Callable[[], Milliseconds]
    config: ConfigSnapshot | None = None
    runtime_id: str | None = None
    data_timestamp_provider: Callable[[], Milliseconds | None] = lambda: None
    data_range: Fact | None = None
    facts_provider: Callable[[], SessionSummaryFacts] | None = None
    _record: RunRecord | None = None
    _identity: RuntimeIdentity | None = None

    # ------------------------------------------------------------------ 生命周期

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode):
            raise RuntimeSessionError("RuntimeSession.mode must be a RuntimeMode")
        for name in ("environment", "venue", "symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise RuntimeSessionError(f"RuntimeSession.{name} must be a non-empty string")
        if not isinstance(self.registry, JsonRunRegistry):
            raise RuntimeSessionError("RuntimeSession.registry must be a JsonRunRegistry")
        if not callable(self.clock):
            raise RuntimeSessionError("RuntimeSession.clock must be callable")

    @property
    def run_id(self) -> str:
        assert self._record is not None, "session has not started"  # noqa: S101
        return self._record.run_id

    @property
    def identity(self) -> RuntimeIdentity:
        if self._identity is None:
            raise RuntimeSessionError("runtime identity is only available after start()")
        return self._identity

    @property
    def record(self) -> RunRecord:
        if self._record is None:
            raise RuntimeSessionError("session has not started")
        return self._record

    @property
    def active(self) -> bool:
        return self._record is not None and self._record.status is RunStatus.RUNNING

    def start(self) -> RunRecord:
        """建立 runtime identity 并 create_run()（重复 start 或 run_id 冲突 ⇒ fail closed）。"""
        if self._record is not None:
            raise RuntimeSessionError(f"session already started (run {self._record.run_id!r})")
        started_at = int(self.clock())
        runtime_id = self.runtime_id or f"{self.mode.value.lower()}-{started_at}"
        if self.registry.load(runtime_id) is not None:
            raise RuntimeSessionError(f"run id {runtime_id!r} already exists in the registry (refusing to reuse)")
        data_ts = self.data_timestamp_provider()
        identity = RuntimeIdentity(
            mode=self.mode, environment=self.environment, venue=self.venue, symbol=self.symbol,
            runtime_id=runtime_id, started_at=started_at,
            data_timestamp=(Fact.unknown("no data timestamp recorded yet") if data_ts is None
                            else Fact.of(int(data_ts))),
        )
        record = self.registry.start(runtime=identity, run_id=runtime_id, now_ms=started_at,
                                     config=self.config, data_range=self.data_range)
        self._identity = identity
        self._record = record
        return record

    def stop(self, *, facts: SessionSummaryFacts | None = None,
             status: RunStatus = RunStatus.COMPLETED) -> RunRecord:
        """graceful stop：自动 finalize（默认 COMPLETED），并将已记录事实打包为 RunSummary。"""
        if self._record is None:
            raise RuntimeSessionError("cannot stop a session that never started")
        if self._record.status is not RunStatus.RUNNING:
            raise RuntimeSessionError(f"session already finalized ({self._record.status.value})")
        if self.config is not None:
            current = self.registry.load(self._record.run_id)
            if current is not None and current.config_fingerprint.known \
                    and current.config_fingerprint.value != self.config.fingerprint:
                raise RuntimeSessionError(
                    "config fingerprint changed during the run; refusing to finalize with a different config"
                )
        resolved = facts if facts is not None else (self.facts_provider() if self.facts_provider else None)
        # 有事实就写进 summary（`status` 才是真相：INCOMPLETE 也可以携带"为什么结束"的事实）
        summary = None if resolved is None else self._summary_payload(resolved)
        record = self.registry.finalize(run_id=self._record.run_id, ended_at=int(self.clock()),
                                        summary=summary, status=status)
        self._record = record
        return record

    def _summary_payload(self, facts: SessionSummaryFacts) -> dict[str, object]:
        assert self._record is not None  # noqa: S101
        metrics = compute_metrics(
            owner_facts={"net_pnl": None, "realized_pnl": facts.realized_pnl,
                         "unrealized_pnl": facts.unrealized_pnl, "fees": facts.fees, "funding": None},
            equity_samples=facts.equity_samples,
            realized_trades=facts.realized_trades,
            max_confirmed_exposure=facts.max_confirmed_exposure,
            max_total_exposure=facts.max_total_exposure,
        )
        summary = build_run_summary(
            identity=self.identity, run_id=self._record.run_id, started_at=self._record.started_at,
            telemetry=facts.telemetry, orders=facts.orders, fills=facts.fills, fees=facts.fees,
            realized_pnl=facts.realized_pnl, unrealized_pnl=facts.unrealized_pnl,
            max_exposure=facts.max_total_exposure, final_position=facts.final_position,
            risk_rejects=facts.risk_rejects, readiness_blocks=facts.readiness_blocks,
            anomalies=facts.anomalies, ended_at=int(self.clock()), data_range=facts.data_range,
            config_id=(None if self.config is None else self.config.config_id),
            metrics_payload=metrics_payload(metrics),
            config_fingerprints=({"config": self.config.fingerprint} if self.config is not None else None),
        )
        return summary_to_jsonable(summary)

    # ------------------------------------------------------------------ context manager

    def __enter__(self) -> "RuntimeSession":
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> bool:
        if exc_type is None:
            self.stop()
        else:
            # 崩溃/异常 ⇒ 如实 finalize 为 INCOMPLETE（不冒充 COMPLETED），并继续抛出原异常
            self.stop(status=RunStatus.INCOMPLETE)
        return False


__all__ = ["RuntimeSession", "RuntimeSessionError", "SessionSummaryFacts"]
