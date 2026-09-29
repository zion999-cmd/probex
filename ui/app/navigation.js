/**
 * F-16 Cross-Surface Navigation（UI 侧镜像 `product/navigation.py`）。
 *
 * 契约由测试与 `product/navigation.py` 对齐（surface/detail 键一致）。
 * 目标一律由 canonical id + timestamp 表达，不做字符串搜索。
 */

export const SURFACE_SLUGS = ["monitor", "market", "activity", "performance", "system"];

/** detail slug -> 页面归属（legacy detail view；system 的 section 由 System 页面自己渲染）。 */
export const DETAIL_ROUTES = {
  monitor: { overview: "overview", portfolio: "portfolio", orders: "orders" },
  market: { live: "market", replay: "market", "run-review": "market" },
  activity: { prediction: "prediction", strategy: "strategy", orders: "orders", evidence: "evidence",
    run: "activity" },
  performance: { runs: "runs", metrics: "metrics" },
  system: {
    health: "system", risk: "system", readiness: "system",
    execution: "system", configuration: "system", capabilities: "system",
  },
};

/** legacy detail 页面模块路径（可到达的 detail route；不是死页面）。 */
export const DETAIL_PAGE_MODULES = {
  "monitor/overview": "/ui/pages/overview/page.js",
  "monitor/portfolio": "/ui/pages/portfolio/page.js",
  "monitor/orders": "/ui/pages/orders/page.js",
  "activity/prediction": "/ui/pages/prediction/page.js",
  "activity/strategy": "/ui/pages/strategy/page.js",
  "activity/orders": "/ui/pages/orders/page.js",
  "activity/evidence": "/ui/pages/evidence/page.js",
  "performance/runs": "/ui/pages/runs/page.js",
  "performance/metrics": "/ui/pages/metrics/page.js",
};

/** blocker owner -> System section（Blocker → System 跳转）。 */
export const BLOCKER_SECTION = {
  READINESS: "readiness",
  RISK: "risk",
  STRATEGY: "execution",
  ORCHESTRATOR: "execution",
  MARKET: "health",
  PREDICTION: "health",
  EXECUTION: "execution",
  VENUE: "execution",
};

export function surfaceModule(surface) {
  return `/ui/pages/${surface}/page.js`;
}

/** 解析 hash → { surface, detail, args, module }；detail 命中 legacy page 时切换模块。 */
export function resolveRoute(surface, rest) {
  const detail = rest[0];
  const key = detail ? `${surface.slug}/${detail}` : null;
  if (key && DETAIL_PAGE_MODULES[key]) {
    return { surface, detail, args: rest.slice(1), module: DETAIL_PAGE_MODULES[key] };
  }
  return { surface, detail: detail || null, args: rest, module: surfaceModule(surface.slug) };
}

export function surfaceHash(surface, detail, identity) {
  const parts = [surface];
  if (detail) parts.push(detail);
  if (identity !== undefined && identity !== null && identity !== "") parts.push(String(identity));
  return `#/${parts.join("/")}`;
}

/** Order / Fill / Execution event → Activity / Evidence（canonical identity）。 */
export function entityHash(kind, identity) {
  if (kind === "run") return surfaceHash("performance", "runs", identity);
  if (kind === "blocker") return surfaceHash("system", BLOCKER_SECTION[identity] || "execution");
  const route = { order: ["activity", "evidence"], fill: ["activity", "evidence"],
    execution_event: ["activity", "evidence"], decision: ["activity", "strategy"],
    prediction: ["activity", "prediction"], evidence: ["activity", "evidence"] }[kind];
  if (!route) return surfaceHash("activity", "evidence", identity);
  return surfaceHash(route[0], route[1], identity);
}

/** Activity event → Market 对应时间点（timestamp，不是字符串搜索）。 */
export function marketPointHash(ts) {
  return surfaceHash("market", "live", ts);
}
