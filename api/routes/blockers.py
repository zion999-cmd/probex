"""`GET /api/v1/blockers`：统一阻塞投影（P0001.11 §4）。只读，不重排、不丢弃。"""

from __future__ import annotations

PATH = "/api/v1/blockers"
SECTIONS = ("runtime", "blockers")


def payload(snapshot: dict) -> dict:
    missing = [section for section in SECTIONS if section not in snapshot]
    if missing:
        raise KeyError(f"snapshot is missing sections: {missing}")
    return {section: snapshot[section] for section in SECTIONS}
