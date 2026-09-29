"""Action Plane 端点（P0001.12.3 §3/§4）：

- `GET  /api/v1/actions`          能力清单（AI 能做什么的**唯一**正式来源）
- `GET  /api/v1/actions/audit`    产品操作审计
- `POST /api/v1/actions/<id>`     受控执行（Confirmation / Policy 由 Gateway 负责）
"""

from __future__ import annotations

PATH = "/api/v1/actions"
AUDIT_PATH = "/api/v1/actions/audit"
SECTIONS = ()


def payload(snapshot: dict) -> dict:  # pragma: no cover - 由 server 直接处理
    raise KeyError("actions are served from the Action Gateway")
