"""Run Registry 端点（P0001.11 §2）：`/api/v1/runs`、`/api/v1/runs/<id>`、`/api/v1/runs/compare`。"""

from __future__ import annotations

PATH = "/api/v1/runs"
SECTIONS = ()
COMPARE_SUFFIX = "/compare"


def payload(snapshot: dict) -> dict:  # pragma: no cover - 该端点由 server 直接生成
    raise KeyError("runs are served from the run registry")
