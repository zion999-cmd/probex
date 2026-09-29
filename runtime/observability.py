"""Structured logging + secret redaction (closure Slice 5 / F-15).

纪律：

- **stdlib logging only**（不引入第三方 logging framework）；
- 每条事件至少含 `ts / level / event / component`，并尽可能带 `runtime_id / run_id / mode /
  reason_code`；同一 owner（`probex` logger）产生，不与 Product facts 混在一起；
- **secret 永不进入日志**：敏感字段名一律 redact；已注册的 secret 值在任意字符串中替换为
  `***redacted***`；`signature=` query 值同样 redact；
- 不改变任何业务语义：日志失败绝不影响调用方（observer 隔离，延续 D-041）。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from io import TextIOBase

LOGGER_NAME = "probex"
REDACTED = "***redacted***"

#: 敏感字段名（大小写不敏感；值一律 redact）
_SENSITIVE_KEY = re.compile(
    r"(token|secret|api[_-]?key|apikey|signature|authorization|password|passphrase|credential)",
    re.IGNORECASE,
)
_SIGNATURE_QUERY = re.compile(r"(signature=)[^&\s\"']+", re.IGNORECASE)
_BEARER = re.compile(r"(bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)

#: 未配置前不向 stderr 泄漏半结构化输出（stdlib 推荐：NullHandler）
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())

#: 已注册的 secret 值（永不写出；用于字符串级替换）
_REGISTERED_SECRETS: set[str] = set()

#: 允许的 level 名
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True, slots=True)
class LoggingPosture:
    """logging 配置姿态（供 System / ops 展示；不参与任何判定）。"""

    level: str
    sink: str                 # "stderr" | "file" | "stream"
    structured: bool
    file_path: str | None = None
    file_max_bytes: int | None = None
    backup_count: int | None = None
    bounded: bool = False
    notes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {"level": self.level, "sink": self.sink, "structured": self.structured,
                "file_path": self.file_path, "file_max_bytes": self.file_max_bytes,
                "backup_count": self.backup_count, "bounded": self.bounded, "notes": list(self.notes)}


def register_secret(value: object) -> None:
    """登记一个 secret 值：之后任何字符串里出现它都会被 redact（防御性）。"""
    if value is None:
        return
    text = str(value)
    if text:
        _REGISTERED_SECRETS.add(text)


def clear_registered_secrets() -> None:
    """仅测试用：清空已登记 secret（不改变运行语义）。"""
    _REGISTERED_SECRETS.clear()


def redact_text(text: object) -> str:
    out = str(text)
    for secret in _REGISTERED_SECRETS:
        if secret:
            out = out.replace(secret, REDACTED)
    out = _SIGNATURE_QUERY.sub(rf"\1{REDACTED}", out)
    return _BEARER.sub(rf"\1{REDACTED}", out)


def sanitize(value: object) -> object:
    """递归清洗字段：敏感 key web 一律 redact；字符串做 secret/signature 替换。"""
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            if _SENSITIVE_KEY.search(name):
                cleaned[name] = REDACTED
            else:
                cleaned[name] = sanitize(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class StructuredJsonFormatter(logging.Formatter):
    """单行 JSON：稳定字段 + `fields`（全部经过 sanitize）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": int(record.created * 1000),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
            "component": getattr(record, "component", record.name),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            cleaned = sanitize(fields)
            if isinstance(cleaned, Mapping):
                payload.update(cleaned)
        if record.exc_info is not None:
            exception = record.exc_info[1]
            payload["exception_type"] = type(exception).__name__
            payload["exception_message"] = redact_text(str(exception))
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str, sort_keys=False)


_configured: LoggingPosture | None = None


def configure_logging(*, level: str = "INFO", stream: TextIOBase | None = None,
                      file_path: str | None = None, file_max_bytes: int | None = None,
                      backup_count: int | None = None) -> LoggingPosture:
    """安装唯一的 `probex` structured handler（幂等：重复调用替换 handler 并返回当前姿态）。"""
    global _configured
    level = str(level).upper()
    if level not in LEVELS:
        raise ValueError(f"log level must be one of {LEVELS}, got {level!r}")
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响配置
            pass
    notes: list[str] = []
    bounded = False
    sink = "stderr" if stream is None else "stream"
    if file_path:
        sink = "file"
        if file_max_bytes is not None:
            if isinstance(file_max_bytes, bool) or int(file_max_bytes) <= 0:
                raise ValueError("file_max_bytes must be a positive int when given")
            handler: logging.Handler = logging.handlers.RotatingFileHandler(
                file_path, maxBytes=int(file_max_bytes),
                backupCount=int(backup_count or 0), encoding="utf-8")
            bounded = True
            notes.append(f"rotating file log (maxBytes={int(file_max_bytes)}, backupCount={int(backup_count or 0)})")
        else:
            handler = logging.FileHandler(file_path, encoding="utf-8")
            notes.append("file log without size bound (UNBOUNDED)")
    else:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(StructuredJsonFormatter())
    logger.addHandler(handler)
    if backup_count is not None and file_path is None:
        notes.append("backup_count ignored without a file log sink")
    _configured = LoggingPosture(level=level, sink=sink, structured=True, file_path=file_path,
                                 file_max_bytes=(int(file_max_bytes) if file_max_bytes else None),
                                 backup_count=(int(backup_count) if backup_count is not None else None),
                                 bounded=bounded, notes=tuple(notes))
    return _configured


def logging_posture() -> LoggingPosture:
    """当前 logging 姿态；未配置 ⇒ 诚实报告 stderr/unstructured-未安装（不是 bounded）。"""
    if _configured is not None:
        return _configured
    return LoggingPosture(level="UNSET", sink="stderr", structured=False, bounded=False,
                          notes=("logging has not been configured yet",))


def reset_logging() -> None:
    """仅测试用：移除 handler 并清除姿态。"""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    logger.addHandler(logging.NullHandler())
    _configured = None


def log_event(component: str, event: str, *, level: int = logging.INFO,
              reason_code: str | None = None, **fields: object) -> None:
    """发出结构化事件；**绝不抛出**（日志失败不得影响业务路径，延续 D-041）。"""
    try:
        logger = logging.getLogger(f"{LOGGER_NAME}.{component}")
        payload: dict[str, object] = {"component": component}
        if reason_code is not None:
            payload["reason_code"] = reason_code
        payload.update(fields)
        logger.log(level, event, extra={"event": event, "component": component, "fields": payload})
    except Exception:  # noqa: BLE001 - observer 边界
        return


__all__ = [
    "LEVELS", "LOGGER_NAME", "LoggingPosture", "REDACTED", "StructuredJsonFormatter",
    "clear_registered_secrets", "configure_logging", "log_event", "logging_posture", "redact_text",
    "register_secret", "reset_logging", "sanitize",
]
