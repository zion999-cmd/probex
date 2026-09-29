"""`GET /api/v1/ops`：operational posture（F-12 / F-15）。只读，不参与任何决策。"""

from __future__ import annotations

PATH = "/api/v1/ops"
SECTIONS = ("runtime", "ops")


def payload(snapshot: dict) -> dict:
    missing = [section for section in SECTIONS if section not in snapshot]
    if missing:
        raise KeyError(f"snapshot is missing sections: {missing}")
    return {section: snapshot[section] for section in SECTIONS}
