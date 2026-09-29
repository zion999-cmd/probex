"""Run Summary 报告层（P0001.10.2 §4 / §5）：JSON + Markdown，确定性输出。"""

from reports.builder import build_run_summary
from reports.json import summary_to_json, summary_to_jsonable
from reports.markdown import summary_to_markdown
from reports.types import RunIdentity, RunSummary

__all__ = [
    "RunIdentity",
    "RunSummary",
    "build_run_summary",
    "summary_to_json",
    "summary_to_jsonable",
    "summary_to_markdown",
]
