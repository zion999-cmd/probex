"""High-Watermark 的**窄**持久化接口（P0001.9.4.2 §5 / §7 / §8）。

纪律：

- **只有一个状态、一个文件**：不引入通用数据库 / checkpoint 框架 / 分布式状态服务；
- 写入必须 `write temp → flush → fsync → atomic replace`：**新 peak 在 durable write 成功之前不得对外发布**
  （§7 的 crash 边界）；
- 读取严格 fail closed：文件缺失 ⇒ `None`（= 从未建立，需要**显式** activation，绝不自动用当前 equity 初始化）；
  内容损坏 / schema 版本不支持 / 字段非法 ⇒ 抛错（调用方必须 BLOCKED）；
- 沿用既有 storage 惯例：`SCHEMA_VERSION` 整数、canonical JSON、错误类型明确。
"""

from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from storage.events.codec import canonical_json
from storage.events.errors import EventStoreError
from risk.high_watermark import EquityHighWatermarkState, HighWatermarkError, state_from_mapping, state_to_mapping

#: 状态文件 schema 版本（与 Event Store 各自独立）。
SCHEMA_VERSION = 1


class HighWatermarkStoreError(EventStoreError):
    """durable store 失败（读取损坏 / 写入失败）。调用方必须 fail closed。"""


class HighWatermarkStore(ABC):
    """窄接口：`load()` / `save(state)`。"""

    @abstractmethod
    def load(self) -> EquityHighWatermarkState | None:
        """读取状态；**不存在返回 None**（从未建立），损坏则抛错。"""

    @abstractmethod
    def save(self, state: EquityHighWatermarkState) -> None:
        """原子持久化；失败抛错（调用方不得继续认为 HWM ACTIVE）。"""


class JsonHighWatermarkStore(HighWatermarkStore):
    """单文件 JSON adapter（atomic replace + fsync）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ 读

    def load(self) -> EquityHighWatermarkState | None:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HighWatermarkStoreError(f"high-watermark state is unreadable: {type(exc).__name__}") from exc
        if not isinstance(raw, dict):
            raise HighWatermarkStoreError("high-watermark state must be a JSON object")
        schema_version = raw.get("schema_version")
        if schema_version != SCHEMA_VERSION:
            raise HighWatermarkStoreError(
                f"unsupported high-watermark schema_version {schema_version!r} (expected {SCHEMA_VERSION})"
            )
        try:
            return state_from_mapping(raw)
        except HighWatermarkError as exc:
            raise HighWatermarkStoreError(f"high-watermark state is invalid: {exc}") from None

    # ------------------------------------------------------------------ 写

    def save(self, state: EquityHighWatermarkState) -> None:
        """原子写：临时文件 → flush → fsync → `os.replace` → fsync 目录。"""
        if not isinstance(state, EquityHighWatermarkState):
            raise HighWatermarkStoreError("save() requires an EquityHighWatermarkState")
        payload = {"schema_version": SCHEMA_VERSION, **state_to_mapping(state)}
        text = canonical_json(payload)
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", dir=directory, prefix=".hwm-", suffix=".tmp", delete=False
            )
            tmp_path = Path(handle.name)
            try:
                handle.write(text)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(tmp_path, self._path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise HighWatermarkStoreError(f"high-watermark state write failed: {type(exc).__name__}") from exc


__all__ = [
    "SCHEMA_VERSION",
    "HighWatermarkStore",
    "HighWatermarkStoreError",
    "JsonHighWatermarkStore",
]
