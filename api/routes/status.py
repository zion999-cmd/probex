"""`GET /api/v1/status`：只读切片（P0001.10 §2）。

产品 API **只组合事实**：本模块不做任何计算，只从 snapshot 中挑选对应段落，
并把 runtime identity 一并带上（§3：UI 不需要猜当前模式）。
"""

from __future__ import annotations

PATH = "/api/v1/status"
SECTIONS = ("runtime", "health",)


def payload(snapshot: dict) -> dict:
    """从序列化后的 snapshot 中取本路由需要的段落（缺失 ⇒ 视为契约错误）。"""
    missing = [section for section in SECTIONS if section not in snapshot]
    if missing:
        raise KeyError(f"snapshot is missing sections: {missing}")
    return {section: snapshot[section] for section in SECTIONS}
