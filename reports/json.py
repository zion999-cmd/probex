"""Run Summary → JSON（P0001.10.2 §4）：复用产品序列化契约（Fact 显式、拒绝 NaN）。"""

from __future__ import annotations

import json

from product.serialization import to_jsonable
from reports.types import RunSummary


def summary_to_jsonable(summary: RunSummary) -> dict[str, object]:
    if not isinstance(summary, RunSummary):
        raise TypeError("summary_to_jsonable requires a RunSummary")
    payload = to_jsonable(summary)
    assert isinstance(payload, dict)  # noqa: S101 - dataclass 必然映射成 dict
    return payload


def summary_to_json(summary: RunSummary, *, indent: int | None = 2) -> str:
    """确定性 JSON：同一输入 ⇒ 同一字节序列。"""
    return json.dumps(summary_to_jsonable(summary), ensure_ascii=False, allow_nan=False,
                      sort_keys=True, indent=indent)
