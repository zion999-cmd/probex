"""Run Registry 的持久化实现（P0001.11 §2 / 裁决 B）。

形态：**append-only index + 每 run 一份 final record**（仓库外目录，默认 `~/.probex/runs/`）。

- `index.jsonl`：只追加，一行一个事件（`start` / `finalize`），canonical JSON + `O_APPEND` 单次写 + `fsync`；
- `runs/<run_id>.json`：finalize 时以 `temp → fsync → os.replace → fsync(dir)` 原子落盘；
- 崩溃未 finalize ⇒ 读出来是 `INCOMPLETE`（**不是** UNKNOWN，也不覆盖历史）；
- 单 writer 语义（裁决 B：本阶段并发不在范围内）；
- 不自动删除（无 retention）。

`compare()` 只比较**同名 metric**，且一边 UNKNOWN 就返回 delta = UNKNOWN。
"""

from __future__ import annotations

import json
import json as _json
import os
from dataclasses import dataclass
from pathlib import Path

from market.events.types import Milliseconds

from product.provenance import ConfigSnapshot
from product.types import Fact, RuntimeIdentity
from reports.types import MetricComparison, RunComparison, RunRecord, RunStatus

#: 仓库外默认目录；可由 env 覆盖（裁决 B）
RUN_REGISTRY_ENV = "PROBEX_RUN_REGISTRY_DIR"
#: active marker 文件名（与 run 同一持久化域；原子更新）
ACTIVE_FILE = "active.json"
DEFAULT_RUN_REGISTRY_DIR = "~/.probex/runs"
INDEX_FILE = "index.jsonl"
RUNS_DIR = "runs"
INDEX_SCHEMA_VERSION = "1"


class RunRegistryError(RuntimeError):
    """Run Registry 契约错误（损坏索引、run 不存在、参数非法）。"""


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _fact_from_payload(payload: object, *, name: str) -> Fact:
    """把已序列化的 `{"known","value","reason"}` 还原成 `Fact`（缺失即 UNKNOWN，绝不填 0）。"""
    if not isinstance(payload, dict) or "known" not in payload:
        return Fact.unknown(f"metric {name!r} is missing in this run record")
    if not payload.get("known"):
        return Fact.unknown(str(payload.get("reason") or "unknown"))
    return Fact.of(payload.get("value"))


@dataclass(slots=True)
class JsonRunRegistry:
    """基于文件系统的 run registry（单 writer）。"""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / RUNS_DIR).mkdir(parents=True, exist_ok=True)
        index = self.root / INDEX_FILE
        if not index.exists():
            handle = os.open(index, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.close(handle)

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_env(cls) -> "JsonRunRegistry":
        return cls(Path(os.environ.get(RUN_REGISTRY_ENV, DEFAULT_RUN_REGISTRY_DIR)))

    # ------------------------------------------------------------------ 写入

    def _append(self, event: dict[str, object]) -> None:
        line = (_canonical({**event, "index_schema_version": INDEX_SCHEMA_VERSION}) + "\n").encode("utf-8")
        handle = os.open(self.root / INDEX_FILE, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(handle, line)
            os.fsync(handle)
        finally:
            os.close(handle)

    def _write_record_file(self, record: RunRecord) -> Path:
        target = self.root / RUNS_DIR / f"{record.run_id}.json"
        temp = target.with_suffix(".json.tmp")
        payload = _canonical(self._record_payload(record))
        handle = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(handle, payload.encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)
        os.replace(temp, target)
        dir_fd = os.open(self.root / RUNS_DIR, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return target

    # ------------------------------------------------------------------ active marker（F-10）

    def mark_active(self, *, run_id: str, runtime_id: str, mode: str, symbol: str,
                    now_ms: Milliseconds, pid: int | None = None) -> dict[str, object]:
        """写入 active marker（原子）：表示**本进程**的 run 正在运行。

        durable RunRecord 的最终状态语义不变；marker 只是"当前有活跃 runtime"的产品读侧事实。
        """
        import os as _os

        marker = {"run_id": run_id, "runtime_id": runtime_id, "mode": mode, "symbol": symbol,
                  "started_at_ms": int(now_ms), "pid": int(pid if pid is not None else _os.getpid())}
        target = self.root / ACTIVE_FILE
        temp = target.with_suffix(".json.tmp")
        handle = _os.open(temp, _os.O_CREAT | _os.O_WRONLY | _os.O_TRUNC, 0o600)
        try:
            _os.write(handle, _json.dumps(marker, sort_keys=True).encode("utf-8"))
            _os.fsync(handle)
        finally:
            _os.close(handle)
        _os.replace(temp, target)
        dir_fd = _os.open(self.root, _os.O_RDONLY)
        try:
            _os.fsync(dir_fd)
        finally:
            _os.close(dir_fd)
        return marker

    def clear_active(self, run_id: str) -> None:
        """清除 active marker（仅当它属于该 run，避免误清别人的）。"""
        marker = self.active_marker()
        if marker is None or marker.get("run_id") != run_id:
            return
        target = self.root / ACTIVE_FILE
        try:
            target.unlink()
        except FileNotFoundError:  # pragma: no cover
            return

    def active_marker(self) -> dict[str, object] | None:
        """读取 active marker（损坏 ⇒ 视为不存在，由读侧按 INCOMPLETE 处理）。"""
        target = self.root / ACTIVE_FILE
        if not target.exists():
            return None
        try:
            payload = _json.loads(target.read_text(encoding="utf-8"))
        except _json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def active_run_id(self) -> str | None:
        """当前活跃 run id：marker 存在**且写出它的进程仍存活**。

        判据优先使用进程身份（pid + runtime identity），不使用 TTL 作为唯一真相；
        pid 复用/异常恢复场景只作为辅助（见文档说明）。
        """
        marker = self.active_marker()
        if marker is None:
            return None
        pid = marker.get("pid")
        if not isinstance(pid, int):
            return None
        try:
            os.kill(pid, 0)                    # 进程存活探测（不发送信号）
        except (OSError, ProcessLookupError):
            return None
        return str(marker.get("run_id"))

    def start(
        self,
        *,
        runtime: RuntimeIdentity,
        run_id: str,
        now_ms: Milliseconds,
        config: ConfigSnapshot | None = None,
        data_range: Fact | None = None,
    ) -> RunRecord:
        """runtime identity 创建时建立 run（裁决 B）。"""
        if not isinstance(runtime, RuntimeIdentity):
            raise RunRegistryError("start() requires a RuntimeIdentity")
        record = RunRecord(
            run_id=run_id, runtime=runtime, started_at=int(now_ms), ended_at=Fact.unknown("run is still active"),
            status=RunStatus.RUNNING,
            config_id=(Fact.unknown("no config snapshot recorded") if config is None else Fact.of(config.config_id)),
            config_fingerprint=(Fact.unknown("no config snapshot recorded") if config is None
                                else Fact.of(config.fingerprint)),
            data_range=(Fact.unknown("data range not recorded") if data_range is None else data_range),
        )
        self._append({"event": "start", "record": self._record_payload(record)})
        return record

    def finalize(
        self,
        *,
        run_id: str,
        ended_at: Milliseconds,
        summary: dict[str, object] | None,
        status: RunStatus = RunStatus.COMPLETED,
    ) -> RunRecord:
        """正常运行结束时**追加** finalization record（不覆盖历史）。"""
        current = self.load(run_id)
        if current is None:
            raise RunRegistryError(f"cannot finalize unknown run {run_id!r}")
        if current.status is RunStatus.COMPLETED:
            raise RunRegistryError(f"run {run_id!r} is already finalized (COMPLETED)")
        record = RunRecord(
            run_id=current.run_id, runtime=current.runtime, started_at=current.started_at,
            ended_at=Fact.of(int(ended_at)), status=status, config_id=current.config_id,
            config_fingerprint=current.config_fingerprint, data_range=current.data_range, summary=summary,
        )
        self._append({"event": "finalize", "record": self._record_payload(record)})
        self._write_record_file(record)
        return record

    # ------------------------------------------------------------------ 读取

    def _record_payload(self, record: RunRecord) -> dict[str, object]:
        from product.serialization import to_jsonable

        payload = to_jsonable(record)
        assert isinstance(payload, dict)  # noqa: S101 - dataclass 必然映射成 dict
        return payload

    def _read_index(self) -> list[dict[str, object]]:
        path = self.root / INDEX_FILE
        events: list[dict[str, object]] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunRegistryError(f"corrupt run index at line {number}: refusing to guess") from exc
            if not isinstance(event, dict) or "event" not in event:
                raise RunRegistryError(f"corrupt run index at line {number}: missing event type")
            events.append(event)
        return events

    @staticmethod
    def _record_from_payload(payload: dict[str, object], *, status: RunStatus) -> RunRecord:
        from product.serialization import to_jsonable  # noqa: F401 - 文档化依赖方向

        runtime_payload = payload.get("runtime")
        if not isinstance(runtime_payload, dict):
            raise RunRegistryError("run index entry is missing the runtime identity")
        runtime = RuntimeIdentity(
            mode=__import__("product.types", fromlist=["RuntimeMode"]).RuntimeMode(runtime_payload["mode"]),
            environment=str(runtime_payload["environment"]),
            venue=str(runtime_payload["venue"]),
            symbol=str(runtime_payload["symbol"]),
            runtime_id=str(runtime_payload["runtime_id"]),
            started_at=int(runtime_payload["started_at"]),
            data_timestamp=_fact_from_payload(runtime_payload.get("data_timestamp"), name="data_timestamp"),
        )
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else None
        return RunRecord(
            run_id=str(payload["run_id"]), runtime=runtime, started_at=int(payload["started_at"]),
            ended_at=_fact_from_payload(payload.get("ended_at"), name="ended_at"), status=status,
            config_id=_fact_from_payload(payload.get("config_id"), name="config_id"),
            config_fingerprint=_fact_from_payload(payload.get("config_fingerprint"), name="config_fingerprint"),
            data_range=_fact_from_payload(payload.get("data_range"), name="data_range"),
            summary=summary,
        )

    def load(self, run_id: str) -> RunRecord | None:
        """读取一个 run：优先 final record 文件；否则由 index 推导（未 finalize ⇒ `INCOMPLETE`）。"""
        if not isinstance(run_id, str) or not run_id:
            raise RunRegistryError("load() requires a non-empty run_id")
        target = self.root / RUNS_DIR / f"{run_id}.json"
        if target.exists():
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RunRegistryError(f"corrupt run record {run_id!r}: refusing to guess") from exc
            if not isinstance(payload, dict):
                raise RunRegistryError(f"corrupt run record {run_id!r}: not an object")
            # record 文件里的 status 才是事实（COMPLETED / INCOMPLETE 都必须如实保留）
            raw_status = payload.get("status") or RunStatus.COMPLETED.value
            try:
                status = RunStatus(str(raw_status))
            except ValueError as exc:
                raise RunRegistryError(f"corrupt run record {run_id!r}: unknown status {raw_status!r}") from exc
            return self._record_from_payload(payload, status=status)
        # 无 finalization：若该 run 正是当前活跃 run（进程存活）⇒ 读侧为 RUNNING（F-10）
        started: dict[str, object] | None = None
        for event in self._read_index():
            payload = event.get("record")
            if not isinstance(payload, dict) or payload.get("run_id") != run_id:
                continue
            if event.get("event") == "start":
                started = payload
            elif event.get("event") == "finalize":
                return self._record_from_payload(payload, status=RunStatus.COMPLETED)
        if started is None:
            return None
        # start 存在但没有 finalize：活跃（本进程仍在跑）⇒ RUNNING；否则 ⇒ INCOMPLETE（裁决 B）
        status = RunStatus.RUNNING if self.active_run_id() == run_id else RunStatus.INCOMPLETE
        return self._record_from_payload(started, status=status)

    def list(self, *, limit: int | None = None) -> tuple[RunRecord, ...]:
        """列出 run（按 started_at 新的在前；只读，不删除任何东西）。"""
        seen: dict[str, RunRecord] = {}
        for event in self._read_index():
            payload = event.get("record")
            if not isinstance(payload, dict) or "run_id" not in payload:
                continue
            run_id = str(payload["run_id"])
            if event.get("event") == "start":
                seen[run_id] = self._record_from_payload(payload, status=RunStatus.RUNNING)
            elif event.get("event") == "finalize":
                seen[run_id] = self._record_from_payload(payload, status=RunStatus.COMPLETED)
        active = self.active_run_id()
        records = [self.load(run_id) or record for run_id, record in seen.items()]
        records.sort(key=lambda record: (record.started_at, record.run_id), reverse=True)
        return tuple(records if limit is None else records[:limit])

    def compare(self, left_run_id: str, right_run_id: str) -> RunComparison:
        """按同名 metric 对齐比较两个 run（一边 UNKNOWN ⇒ delta = UNKNOWN）。"""
        left = self.load(left_run_id)
        right = self.load(right_run_id)
        if left is None:
            raise RunRegistryError(f"unknown run {left_run_id!r}")
        if right is None:
            raise RunRegistryError(f"unknown run {right_run_id!r}")
        left_metrics = (left.summary or {}).get("metrics") if isinstance(left.summary, dict) else None
        right_metrics = (right.summary or {}).get("metrics") if isinstance(right.summary, dict) else None
        names: list[str] = []
        for source in (left_metrics, right_metrics):
            if isinstance(source, dict):
                names.extend(str(name) for name in source)
        comparisons: list[MetricComparison] = []
        for name in sorted(set(names)):
            left_fact = _fact_from_payload((left_metrics or {}).get(name) if isinstance(left_metrics, dict)
                                           else None, name=name)
            right_fact = _fact_from_payload((right_metrics or {}).get(name) if isinstance(right_metrics, dict)
                                            else None, name=name)
            comparisons.append(MetricComparison(name=name, left=left_fact, right=right_fact,
                                                delta=_delta(name, left_fact, right_fact)))
        return RunComparison(left_run_id=left_run_id, right_run_id=right_run_id, metrics=tuple(comparisons))


def _delta(name: str, left: Fact, right: Fact) -> Fact:
    if not left.known or not right.known:
        return Fact.unknown(f"delta undefined: {name!r} is UNKNOWN on one side "
                            f"(left={left.reason or 'known'}, right={right.reason or 'known'})")
    if not isinstance(left.value, (int, float)) or isinstance(left.value, bool) \
            or not isinstance(right.value, (int, float)) or isinstance(right.value, bool):
        return Fact.unknown(f"delta undefined: {name!r} is not numeric")
    return Fact.of(right.value - left.value)


__all__ = [
    "DEFAULT_RUN_REGISTRY_DIR",
    "INDEX_FILE",
    "INDEX_SCHEMA_VERSION",
    "RUNS_DIR",
    "RUN_REGISTRY_ENV",
    "JsonRunRegistry",
    "RunRegistryError",
]
