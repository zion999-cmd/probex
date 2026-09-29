"""产品序列化（P0001.10 §2）：**内部 Python 类型不得裸序列化**。

规则：

- 统一信封：`{"schema_version": ..., "generated_at": ..., "runtime": {...}, "<section>": {...}}`；
- `Fact` 一律序列化为 `{"known": bool, "value": ..., "reason": ...}` ⇒ UNKNOWN 在 JSON 中**显式可见**，
  永远不会变成 0 / `[]` / `"healthy"`；
- NaN / Infinity 一律拒绝（`allow_nan=False`），避免把"坏值"伪装成合法 JSON。
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields, is_dataclass
from enum import Enum

from product.snapshot import SystemSnapshot
from product.types import Fact


class SerializationError(TypeError):
    """产品序列化契约错误。"""


def fact_to_jsonable(fact: Fact) -> dict[str, object]:
    if not isinstance(fact, Fact):
        raise SerializationError("fact_to_jsonable requires a Fact")
    value = fact.value
    if isinstance(value, Enum):
        value = value.value
    return {"known": fact.known, "value": value, "reason": fact.reason}


def to_jsonable(value: object) -> object:
    """把产品类型递归转成 JSON 友好的纯数据（严格：未知类型直接报错，不猜测）。"""
    if isinstance(value, Fact):
        return fact_to_jsonable(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise SerializationError("NaN/Infinity is not a valid product value (unknown must be explicit)")
        return value
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclass_fields(value)}
    raise SerializationError(f"cannot serialize {type(value).__name__} into the product contract")


def snapshot_to_jsonable(snapshot: SystemSnapshot) -> dict[str, object]:
    if not isinstance(snapshot, SystemSnapshot):
        raise SerializationError("snapshot_to_jsonable requires a SystemSnapshot")
    payload = to_jsonable(snapshot)
    assert isinstance(payload, dict)  # noqa: S101 - dataclass 必然映射成 dict
    _annotate_reasons(payload)
    return payload


def _annotate_reasons(payload: dict[str, object]) -> None:
    """F-09：给已序列化的 blocker 附加人类解释（原始 reason_code 保留）。

    presentation-only：不改变任何决策，不替换 code；未知 code 走 `explain_reason` 的兜底文案。
    """
    from product.reason_catalog import explain_reason

    blockers = payload.get("blockers")
    if not isinstance(blockers, list):
        return
    for entry in blockers:
        if isinstance(entry, dict) and isinstance(entry.get("reason_code"), str):
            entry["explanation"] = explain_reason(entry["reason_code"]).to_payload()


def snapshot_to_json(snapshot: SystemSnapshot, *, indent: int | None = None) -> str:
    return json.dumps(snapshot_to_jsonable(snapshot), ensure_ascii=False, allow_nan=False,
                      sort_keys=False, indent=indent)
