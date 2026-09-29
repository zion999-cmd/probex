/**
 * Probex Product Surface Architecture（P0001.12.1）——**信息架构的单一来源**。
 *
 * 一级导航固定为 5 个 Surface；每个 Surface 回答一个用户问题。
 * 旧页面（prediction / strategy / orders / portfolio / risk / readiness / evidence / runs）
 * 不再是一级导航，而是 Surface 内部的 detail view（能力不删除，只改信息架构）。
 */

export const SURFACES = [
  {
    slug: "monitor",
    title: "Monitor",
    question: "现在系统怎么样？",
    detail: ["overview", "portfolio", "orders"],
    evidence: [],
  },
  {
    slug: "market",
    title: "Market",
    question: "市场现在发生了什么？Probex 看到了什么？",
    detail: ["market-live", "market-replay", "market-run-review"],
    evidence: ["market-health", "market-overlays"],
  },
  {
    slug: "activity",
    title: "Activity",
    question: "系统为什么这样做？",
    detail: ["prediction", "strategy", "orders"],
    evidence: ["evidence"],
  },
  {
    slug: "performance",
    title: "Performance",
    question: "系统做得怎么样？",
    detail: ["runs", "run-detail", "run-compare"],
    evidence: ["metrics"],
  },
  {
    slug: "system",
    title: "System",
    question: "为什么系统现在能运行，或者为什么不能运行？",
    detail: ["health", "risk", "readiness", "execution", "configuration", "capabilities"],
    evidence: [],
  },
];

/**
 * 旧页面 → 新 Surface（SC-10：不得出现孤儿页面）。
 *
 * F-16：`risk` / `readiness` / `capabilities` 已被 System 的 section **完全覆盖**，因此删除；
 * 其余旧页面保留为可到达的 detail route（见 `app/navigation.js` DETAIL_PAGE_MODULES）。
 */
export const LEGACY_PAGE_HOME = {
  overview: "monitor",
  portfolio: "monitor",
  prediction: "activity",
  strategy: "activity",
  orders: "activity",
  evidence: "activity",
  runs: "performance",
  metrics: "performance",
};

/** 全局 Header 的永久字段（Mode 必须永远明显，SC：Mode 显式）。 */
export const HEADER_FIELDS = ["mode", "environment", "venue", "symbol", "runtime_id", "health", "data_timestamp"];

/** 仅用于 UI 展示的 Surface 标签（语义由本文件固定，视觉样式由实现层决定）。 */
export function surfaceForHash(hash) {
  const slug = String(hash || "").replace(/^#\/?/, "").split("/")[0];
  return SURFACES.find((surface) => surface.slug === slug) || SURFACES[0];
}
