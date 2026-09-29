"""Config Provenance（P0001.11 §1 / 裁决 C）："这次运行到底用了哪组配置"。

纪律：

- **只记录非敏感 resolved values**；secret **只记录引用名**（如 `env:BINANCE_API_KEY`），永不保存 secret value；
- **业务值仍不得有隐式默认**：本模块只在调用方给出的候选项之间**按优先级选择**，绝不合成新值、绝不填默认；
- `fingerprint` = canonical JSON（排序、紧凑）+ schema 版本 + 非敏感 resolved values + secret reference names；
- 本模块不读取任何 `.env` 文件、不触网、不含可用凭据。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from market.events.types import Milliseconds

from product.types import SCHEMA_VERSION, Fact

#: provenance 自身的 schema 版本（参与 fingerprint）
PROVENANCE_SCHEMA_VERSION = "1"

#: secret 引用名的允许形态（只允许引用，不允许值）
SECRET_REF_PATTERN = re.compile(r"^(env:[A-Z][A-Z0-9_]*|file:[^\s]+|credential:[^\s]+)$")

_SECRET_VALUE_REASON = "sensitive value is never stored in config provenance"


class ConfigProvenanceError(ValueError):
    """配置来源契约错误（重复来源、非法 secret 引用等）。"""


class ConfigSource(Enum):
    """配置来源分类法（第一版固定四类；优先级 CLI > ENV > FILE > CONSTRUCTOR）。"""

    CLI = "CLI"
    ENV = "ENV"
    FILE = "FILE"
    CONSTRUCTOR = "CONSTRUCTOR"


#: 优先级（越靠前越优先；仅用于**选择**调用方给出的候选项，不生成默认值）
SOURCE_PRECEDENCE: tuple[ConfigSource, ...] = (
    ConfigSource.CLI,
    ConfigSource.ENV,
    ConfigSource.FILE,
    ConfigSource.CONSTRUCTOR,
)


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    """一个配置项的来源与（非敏感）解析值。"""

    name: str
    source: ConfigSource
    value: Fact
    secret_ref: str | None = None
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ConfigProvenanceError("ConfigEntry.name must be a non-empty string")
        if not isinstance(self.source, ConfigSource):
            raise ConfigProvenanceError("ConfigEntry.source must be a ConfigSource")
        if not isinstance(self.value, Fact):
            raise ConfigProvenanceError("ConfigEntry.value must be a Fact")
        if not isinstance(self.sensitive, bool):
            raise ConfigProvenanceError("ConfigEntry.sensitive must be a bool")
        if self.secret_ref is not None and not SECRET_REF_PATTERN.match(self.secret_ref):
            raise ConfigProvenanceError(
                f"ConfigEntry.secret_ref must look like 'env:NAME' / 'file:...' / 'credential:...', "
                f"got {self.secret_ref!r}"
            )
        if self.sensitive and self.value.known:
            # 敏感值绝不进入 provenance（不是"尽量"，是硬约束）
            raise ConfigProvenanceError(
                f"ConfigEntry[{self.name}] is sensitive: value must stay UNKNOWN ({_SECRET_VALUE_REASON})"
            )


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """一次运行的配置事实快照（Config Provenance 的产品形态）。"""

    config_id: str
    entries: tuple[ConfigEntry, ...]
    fingerprint: str
    created_at: Milliseconds
    schema_version: str = SCHEMA_VERSION
    provenance_schema_version: str = PROVENANCE_SCHEMA_VERSION

    def sources(self) -> dict[str, int]:
        """按来源统计条目数（供 UI/报告展示"配置从哪来"）。"""
        counts: dict[str, int] = {source.value: 0 for source in ConfigSource}
        for entry in self.entries:
            counts[entry.source.value] += 1
        return counts

    def secret_refs(self) -> tuple[str, ...]:
        return tuple(sorted({entry.secret_ref for entry in self.entries if entry.secret_ref}))

    def entry(self, name: str) -> ConfigEntry | None:
        return next((entry for entry in self.entries if entry.name == name), None)


def resolve_config(entries: tuple[ConfigEntry, ...] | list[ConfigEntry]) -> tuple[ConfigEntry, ...]:
    """按 `CLI > ENV > FILE > CONSTRUCTOR` 选择每个配置项的**唯一**来源。

    - 同一 `(name, source)` 重复出现 ⇒ 抛错（不静默覆盖，避免"两个 FILE 谁赢"的歧义）；
    - 返回顺序固定：先按名字典序，保证 fingerprint 与展示确定性；
    - **不新增任何条目**：没有候选项的配置项就是没有（业务值不得有隐式默认）。
    """
    seen: set[tuple[str, ConfigSource]] = set()
    best: dict[str, ConfigEntry] = {}
    for entry in entries:
        key = (entry.name, entry.source)
        if key in seen:
            raise ConfigProvenanceError(f"duplicate config entry for {entry.name!r} from {entry.source.value}")
        seen.add(key)
        current = best.get(entry.name)
        if current is None or SOURCE_PRECEDENCE.index(entry.source) < SOURCE_PRECEDENCE.index(current.source):
            best[entry.name] = entry
    return tuple(best[name] for name in sorted(best))


def config_fingerprint(entries: tuple[ConfigEntry, ...]) -> str:
    """canonical fingerprint（裁决 C）：schema 版本 + 非敏感 resolved values + secret reference names。"""
    payload = {
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "product_schema_version": SCHEMA_VERSION,
        "entries": [
            {
                "name": entry.name,
                "source": entry.source.value,
                "known": entry.value.known,
                "value": entry.value.value if entry.value.known else None,
                "reason": entry.value.reason,
                "secret_ref": entry.secret_ref,
                "sensitive": entry.sensitive,
            }
            for entry in sorted(entries, key=lambda item: (item.name, item.source.value))
        ],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_config_snapshot(
    *,
    config_id: str,
    entries: tuple[ConfigEntry, ...] | list[ConfigEntry],
    created_at: Milliseconds,
) -> ConfigSnapshot:
    """构造配置快照（会先做优先级解析；重复来源或敏感值泄露 ⇒ 抛错）。"""
    if not isinstance(config_id, str) or not config_id:
        raise ConfigProvenanceError("config_id must be a non-empty string")
    if isinstance(created_at, bool) or not isinstance(created_at, int) or created_at < 0:
        raise ConfigProvenanceError("created_at must be a non-negative int (ms)")
    resolved = resolve_config(tuple(entries))
    return ConfigSnapshot(config_id=config_id, entries=resolved,
                          fingerprint=config_fingerprint(resolved), created_at=int(created_at))


def secret_entry(name: str, *, source: ConfigSource, secret_ref: str) -> ConfigEntry:
    """构造一个**只带引用名**的 secret 配置项（值永远是 UNKNOWN）。"""
    return ConfigEntry(name=name, source=source, value=Fact.unknown(_SECRET_VALUE_REASON),
                       secret_ref=secret_ref, sensitive=True)


__all__ = [
    "PROVENANCE_SCHEMA_VERSION",
    "SECRET_REF_PATTERN",
    "SOURCE_PRECEDENCE",
    "ConfigEntry",
    "ConfigProvenanceError",
    "ConfigSnapshot",
    "ConfigSource",
    "build_config_snapshot",
    "config_fingerprint",
    "resolve_config",
    "secret_entry",
]
