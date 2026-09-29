"""F-16 Cross-Surface Navigation contract（**单一来源**）。

规则：

- 目标由 **canonical id + timestamp** 表达（不做字符串搜索、不猜）；
- Assistant 的 `navigate.surface` / `select.entity` 与 UI 链接使用**同一契约**（UI 侧镜像 `ui/app/navigation.js`，
  由测试固定两侧一致）；
- legacy 页面要么被删除（已完全被 Surface 覆盖），要么在这里登记为 detail route（可到达）。

本模块是纯映射，不含事实、不 import 领域模块、无副作用。
"""

from __future__ import annotations

from dataclasses import dataclass

SURFACES: tuple[str, ...] = ("monitor", "market", "activity", "performance", "system")

#: detail slug -> 页面归属（legacy detail view；`system` 的 section 由 System 页面自己渲染）
DETAIL_ROUTES: dict[str, dict[str, str]] = {
    "monitor": {"overview": "overview", "portfolio": "portfolio", "orders": "orders"},
    "market": {"live": "market", "replay": "market", "run-review": "market"},
    "activity": {"prediction": "prediction", "strategy": "strategy", "orders": "orders",
                 "evidence": "evidence"},
    "performance": {"runs": "runs", "metrics": "metrics"},
    "system": {"health": "system", "risk": "system", "readiness": "system",
               "execution": "system", "configuration": "system", "capabilities": "system"},
}

#: blocker owner -> System section（Blocker → System 跳转）
BLOCKER_SECTION: dict[str, str] = {
    "READINESS": "readiness",
    "RISK": "risk",
    "STRATEGY": "execution",
    "ORCHESTRATOR": "execution",
    "MARKET": "health",
    "PREDICTION": "health",
    "EXECUTION": "execution",
    "VENUE": "execution",
}

#: entity kind -> (surface, detail)（Order/Fill → Evidence/Raw Facts 等）
ENTITY_ROUTES: dict[str, tuple[str, str]] = {
    "order": ("activity", "evidence"),
    "fill": ("activity", "evidence"),
    "execution_event": ("activity", "evidence"),
    "decision": ("activity", "strategy"),
    "prediction": ("activity", "prediction"),
    "evidence": ("activity", "evidence"),
}


class NavigationError(ValueError):
    """导航契约错误（未知 surface / entity kind）。"""


@dataclass(frozen=True, slots=True)
class NavigationTarget:
    """一个只读导航目标（UI 与 Assistant 共用）。"""

    surface: str
    detail: str | None
    identity: str | None
    target: str

    def to_payload(self) -> dict[str, object]:
        return {"surface": self.surface, "detail": self.detail, "identity": self.identity,
                "target": self.target}


def surface_route(surface: str, *, detail: str | None = None,
                  identity: str | None = None) -> NavigationTarget:
    """构造 `#/<surface>[/<detail>][/<identity>]`（surface/detail 必须在契约内）。"""
    if surface not in SURFACES:
        raise NavigationError(f"unknown surface {surface!r} (allowed: {SURFACES})")
    if detail is not None and detail not in DETAIL_ROUTES[surface]:
        raise NavigationError(f"unknown detail {detail!r} for surface {surface!r}")
    parts = ["", surface]
    if detail:
        parts.append(detail)
    if identity:
        parts.append(str(identity))
    return NavigationTarget(surface=surface, detail=detail,
                            identity=(str(identity) if identity else None),
                            target="#/" + "/".join(parts[1:]))


def entity_route(kind: str, identity: str) -> NavigationTarget:
    """按 canonical identity 给出 drill-down 目标（Order/Fill → Activity/Evidence 等）。"""
    if not isinstance(identity, str) or not identity:
        raise NavigationError("entity_route requires a non-empty identity")
    if kind == "run":
        return surface_route("performance", detail="runs", identity=identity)
    if kind == "blocker":
        section = BLOCKER_SECTION.get(identity, "execution")
        return surface_route("system", detail=section)
    route = ENTITY_ROUTES.get(kind)
    if route is None:
        raise NavigationError(f"unknown entity kind {kind!r} (allowed: run, blocker, "
                              f"{sorted(ENTITY_ROUTES)})")
    return surface_route(route[0], detail=route[1], identity=identity)


def market_point_route(ts: object) -> NavigationTarget:
    """Activity event → Market 对应时间点（用 timestamp，不靠字符串搜索）。"""
    try:
        value = int(ts)
    except (TypeError, ValueError):
        raise NavigationError(f"market_point_route requires an integer timestamp, got {ts!r}") from None
    return surface_route("market", detail="live", identity=str(value))


def blocker_route(owner: object) -> NavigationTarget:
    """Blocker → System 对应 section（owner 来自 `BlockerView.owner`）。"""
    name = getattr(owner, "value", owner)
    section = BLOCKER_SECTION.get(str(name), "execution")
    return surface_route("system", detail=section)


def detail_route_keys() -> tuple[str, ...]:
    """`surface/detail` 键（供 UI ⇄ Python 契约测试）。"""
    return tuple(f"{surface}/{detail}" for surface in SURFACES for detail in DETAIL_ROUTES[surface])


__all__ = [
    "BLOCKER_SECTION", "DETAIL_ROUTES", "ENTITY_ROUTES", "NavigationError", "NavigationTarget",
    "SURFACES", "blocker_route", "detail_route_keys", "entity_route", "market_point_route",
    "surface_route",
]
