"""兼容入口：`build_run_summary` / `summary_to_jsonable` 的实现已移至 `builder.py` / `json.py`。"""

from reports.builder import build_run_summary
from reports.json import summary_to_json, summary_to_jsonable
from reports.markdown import summary_to_markdown

__all__ = ["build_run_summary", "summary_to_json", "summary_to_jsonable", "summary_to_markdown"]
