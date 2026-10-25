"""P0017.3 规范化身份与内容 hash（确定性、可复现）。

- canonical_json：sort_keys + 固定分隔，浮点/默认值/字段顺序不影响身份。
- dataset identity：只依赖文件**内容**与规范化的文件清单（不依赖路径/mtime/条数）。
- run identity：由代码版本、数据集身份、生效配置、seed、模型/schema 版本决定。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backtest import BacktestError, BacktestStatus

BACKTEST_SCHEMA = "backtest/v1"
EXECUTION_MODEL = "event-driven-simulated-venue/v1"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_identity(path: Path) -> dict[str, object]:
    """单个输入文件的内容身份。"""
    if not path.is_file():
        raise BacktestError(BacktestStatus.INVALID_INPUT, f"dataset file not found: {path}",
                              location=str(path))
    data = path.read_bytes()
    return {"path": path.name, "bytes": len(data), "sha256": sha256_bytes(data)}


def dataset_identity(files: list[Path]) -> dict[str, object]:
    """多文件数据集身份：规范化（按相对路径排序）后组合 hash。

    同内容 ⇒ 同身份；内容变 ⇒ 身份变；不使用 mtime/路径目录/记录数。
    """
    normalized = sorted((file_identity(p) for p in files),
                         key=lambda item: str(item["path"]))
    digest = sha256_text(canonical_json({"files": normalized}))
    return {"files": normalized, "sha256": digest}


def directory_dataset_identity(root: Path, *, symbols: tuple[str, ...] | None = None
                                ) -> dict[str, object]:
    """共享市场数据集目录的内容身份（P0017.1 布局）。

    规范化所有 `data.jsonl` 分片的**相对路径 + 字节数 + 内容 sha256**（排序后组合 hash）；
    不依赖根路径 / mtime。`symbols` 给出时只计入这些标的（与实际回测输入一致）。
    """
    files: list[Path] = []
    for data_file in sorted(root.rglob("data.jsonl")):
        if symbols is not None:
            parts = data_file.relative_to(root).parts
            # 布局：<venue>/<symbol>/<kind>/<day>/data.jsonl
            if len(parts) < 5 or parts[1] not in symbols:
                continue
        files.append(data_file)
    if not files:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"no dataset partitions found under {root}",
                              location=str(root))
    normalized = sorted(
        ({"path": str(p.relative_to(root)), "bytes": len(p.read_bytes()),
          "sha256": sha256_bytes(p.read_bytes())} for p in files),
        key=lambda item: str(item["path"]))
    digest = sha256_text(canonical_json({"files": normalized}))
    return {"files": normalized, "sha256": digest}


def identity_for_input(path: Path, *, symbols: tuple[str, ...] | None = None
                        ) -> dict[str, object]:
    """回测输入身份：文件 ⇒ 文件身份；目录 ⇒ 数据集分片身份。"""
    if path.is_file():
        return dataset_identity([path])
    if path.is_dir():
        return directory_dataset_identity(path, symbols=symbols)
    raise BacktestError(BacktestStatus.INVALID_INPUT, f"dataset not found: {path}",
                          location=str(path))


def run_identity(*, code_version: str, dataset_sha256: str, config: dict[str, object],
                 seed: int | None, schema: str = BACKTEST_SCHEMA,
                 execution_model: str = EXECUTION_MODEL) -> tuple[str, str]:
    """返回 (run_id, canonical_input_hash)。只改输出目录不影响身份。"""
    material = {
        "code_version": code_version,
        "dataset": dataset_sha256,
        "config": config,
        "seed": seed,
        "schema": schema,
        "execution_model": execution_model,
    }
    canonical = canonical_json(material)
    return f"bt-{sha256_text(canonical)[:16]}", sha256_text(canonical)


__all__ = [
    "BACKTEST_SCHEMA",
    "EXECUTION_MODEL",
    "canonical_json",
    "dataset_identity",
    "directory_dataset_identity",
    "file_identity",
    "identity_for_input",
    "run_identity",
    "sha256_bytes",
    "sha256_text",
]
