"""RuntimeSession 的生产接线（P0001.11.2）。

本模块只做一件事：把 `RuntimeSession` 挂到真实运行入口的**生命周期**上
（start / graceful stop / abnormal termination），**不拥有任何交易决策**。

关键契约（人类裁决）：

- **不重新实现 stop**：`stop_hook` 是原始 Owner 的 stop（live 场景即 `orchestrator.stop`），
  它**先**执行且**仍是**唯一 Owner；`RuntimeSession` 只在其**正常返回之后** finalize run；
- `stop_hook` 抛错（或 stop 未正常完成）⇒ run 记 `INCOMPLETE` 并**重新抛出**，绝不记 `COMPLETED`；
- 本模块不 import `strategy` / `risk` / `execution` / `connectors` / `live`（全部 duck-typed），
  因此它在结构上**没有下单能力**；无 daemon、无线程、无多进程。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from types import TracebackType

from product.types import RuntimeMode
from reports.types import RunRecord, RunStatus
from runtime.session import RuntimeSession, RuntimeSessionError, SessionSummaryFacts


class WiringError(RuntimeError):
    """接线契约错误（缺 stop_hook、会话未启动等）。"""


@dataclass(slots=True)
class SessionHost:
    """把一次真实运行包进 `RuntimeSession`（只监听生命周期）。"""

    session: RuntimeSession
    stop_hook: Callable[[], object] | None = None
    facts_provider: Callable[[], SessionSummaryFacts] | None = None
    _steps: int = 0
    _started: bool = False
    _stopped: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.session, RuntimeSession):
            raise WiringError("SessionHost.session must be a RuntimeSession")

    # ------------------------------------------------------------------ 生命周期

    @property
    def run_id(self) -> str:
        return self.session.run_id

    @property
    def steps(self) -> int:
        """已消费的步数（只读计数，不参与任何决策）。"""
        return self._steps

    def start(self) -> RunRecord:
        """登记 run（runtime identity 建立时调用一次）。"""
        record = self.session.start()
        self._started = True
        return record

    def run_feed(self, feed: Iterable[object], *, step: Callable[[object], object] | None = None) -> int:
        """消费真实事件流（`ReplaySource` 事件、`MarketState` 等），可选每步回调。

        本方法**不解释** feed 的内容，也不改变任何决策顺序：它只按顺序调用 `step(item)` 并计数。
        """
        if not self._started:
            raise WiringError("start() the session before running the feed")
        count = 0
        for item in feed:
            if step is not None:
                step(item)
            count += 1
        self._steps += count
        return count

    def finish(self, *, facts: SessionSummaryFacts | None = None) -> RunRecord:
        """graceful stop：**先**执行原始 Owner 的 `stop_hook`，成功后才 finalize `COMPLETED`。"""
        if self._stopped:
            raise WiringError("finish() was already called on this host")
        if self.stop_hook is not None:
            try:
                self.stop_hook()
            except BaseException:
                # 原始 Owner 的 stop 失败 ⇒ run 记 INCOMPLETE（绝不 COMPLETED），异常继续传播
                self._stopped = True
                self.session.stop(status=RunStatus.INCOMPLETE)
                raise
        self._stopped = True
        return self.session.stop(facts=self._resolve_facts(facts))

    def terminate(self, reason: str, *, status: RunStatus = RunStatus.INCOMPLETE) -> RunRecord:
        """异常终止：如实记 `INCOMPLETE`（默认），并把原因写入 anomalies。"""
        if not isinstance(reason, str) or not reason:
            raise WiringError("terminate() requires a non-empty reason")
        self._stopped = True
        return self.session.stop(facts=self._resolve_facts(None, extra_anomaly=reason), status=status)

    def _resolve_facts(self, facts: SessionSummaryFacts | None,
                       *, extra_anomaly: str | None = None) -> SessionSummaryFacts | None:
        resolved = facts
        if resolved is None and self.facts_provider is not None:
            resolved = self.facts_provider()
        if extra_anomaly is not None:
            base = resolved or SessionSummaryFacts()
            resolved = replace(base, anomalies=base.anomalies + (extra_anomaly,))
        return resolved

    # ------------------------------------------------------------------ context manager

    def __enter__(self) -> "SessionHost":
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> bool:
        if exc_type is None:
            self.finish()
        else:
            # 异常退出：先让原始 Owner 尽力 stop（失败也不掩盖原异常），再如实记 INCOMPLETE
            if self.stop_hook is not None:
                try:
                    self.stop_hook()
                except BaseException:  # noqa: BLE001 - 不能掩盖原始异常
                    pass
            self.terminate(f"{exc_type.__name__}: abnormal termination")
        return False


def open_session(
    *,
    mode: RuntimeMode,
    environment: str,
    venue: str,
    symbol: str,
    session: RuntimeSession,
    stop_hook: Callable[[], object] | None = None,
    facts_provider: Callable[[], SessionSummaryFacts] | None = None,
) -> SessionHost:
    """共用入口：校验 mode 与会话一致后返回 `SessionHost`（四种模式共用同一套语义）。"""
    if not isinstance(mode, RuntimeMode):
        raise WiringError("open_session() requires a RuntimeMode")
    if session.mode is not mode:
        raise WiringError(f"session mode {session.mode.value} does not match requested {mode.value}")
    if not isinstance(environment, str) or not environment:
        raise WiringError("open_session() requires a non-empty environment")
    if session.environment != environment or session.venue != venue or session.symbol != symbol:
        raise WiringError("session identity does not match the requested environment/venue/symbol")
    return SessionHost(session=session, stop_hook=stop_hook, facts_provider=facts_provider)


def _open_expected_mode(expected: RuntimeMode, kwargs: dict[str, object]) -> SessionHost:
    """四个模式入口的共用实现：显式 `mode` 必须与入口一致（否则 fail closed）。"""
    requested = kwargs.pop("mode", expected)
    if requested is not expected:
        raise WiringError(f"this entry point is {expected.value}, got mode={requested}")
    return open_session(mode=expected, **kwargs)  # type: ignore[arg-type]


def open_replay_session(**kwargs: object) -> SessionHost:
    """REPLAY 入口（runtime 层只登记生命周期；组件由调用方注入）。"""
    return _open_expected_mode(RuntimeMode.REPLAY, dict(kwargs))


def open_paper_session(**kwargs: object) -> SessionHost:
    """PAPER 入口。"""
    return _open_expected_mode(RuntimeMode.PAPER, dict(kwargs))


def open_testnet_session(**kwargs: object) -> SessionHost:
    """TESTNET 入口（写授权仍由 readiness/authority 拥有，runtime 层不参与）。"""
    return _open_expected_mode(RuntimeMode.TESTNET, dict(kwargs))


def open_live_session(**kwargs: object) -> SessionHost:
    """LIVE 入口（结构接线；Mainnet 写授权仍由 readiness/authority 拥有）。"""
    return _open_expected_mode(RuntimeMode.LIVE, dict(kwargs))


__all__ = [
    "SessionHost",
    "WiringError",
    "open_live_session",
    "open_paper_session",
    "open_replay_session",
    "open_session",
    "open_testnet_session",
]
