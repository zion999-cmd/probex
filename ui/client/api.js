/** 唯一数据入口：只访问 .10.1 Product API（P0001.10.2 SC-1）。 */
export const ENDPOINTS = {
  schema: "/api/v1/schema",
  snapshot: "/api/v1/snapshot",
  status: "/api/v1/status",
  market: "/api/v1/market",
  prediction: "/api/v1/prediction",
  strategy: "/api/v1/strategy",
  risk: "/api/v1/risk",
  orders: "/api/v1/orders",
  portfolio: "/api/v1/portfolio",
  readiness: "/api/v1/readiness",
  instrument: "/api/v1/instrument",
  decisionOrders: "/api/v1/decisions/orders",
  assistantContext: "/api/v1/assistant/context",
  assistantExplain: "/api/v1/assistant/explain",
  evidence: "/api/v1/evidence",
  runSummary: "/api/v1/reports/run-summary",
  metrics: "/api/v1/metrics",
  capabilities: "/api/v1/capabilities",
  blockers: "/api/v1/blockers",
  runs: "/api/v1/runs",
  runCompare: "/api/v1/runs/compare",
  marketTimeline: "/api/v1/market/timeline",
  marketDepth: "/api/v1/market/depth",
  marketTrades: "/api/v1/market/trades",
  marketHealth: "/api/v1/market/health",
  marketOverlays: "/api/v1/market/overlays",
  marketCandles: "/api/v1/market/candles",
  replay: "/api/v1/replay",
  facts: "/api/v1/facts",
  portfolioTimeline: "/api/v1/portfolio/timeline",
  actions: "/api/v1/actions",
  actionsAudit: "/api/v1/actions/audit",
  assistantContext: "/api/v1/assistant/context",
  executionHealth: "/api/v1/execution/health",
  executionLimits: "/api/v1/execution/limits",
  executionRateLimits: "/api/v1/execution/rate-limits",
  executionLatency: "/api/v1/execution/latency",
  executionAnomalies: "/api/v1/execution/anomalies",
  executionReconciliation: "/api/v1/execution/reconciliation",
  reasons: "/api/v1/reasons",
  ops: "/api/v1/ops",
};

/** F-09：reason catalog 只取一次（UI/Assistant/CLI 共用同一来源）。 */
let _reasonCatalog = null;
export async function reasonCatalog() {
  if (_reasonCatalog === null) {
    try {
      _reasonCatalog = (await fetchJson(ENDPOINTS.reasons)).catalog || [];
    } catch (error) {
      _reasonCatalog = [];
    }
  }
  return _reasonCatalog;
}

/** 唯一允许的 POST：local replay session control（只作用于 REPLAY runtime）。 */
export async function postReplay(verb, payload = {}) {
  const response = await fetch(`${ENDPOINTS.replay}/${verb}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`${verb} -> HTTP ${response.status}: ${body.detail || ""}`);
  return body;
}

export async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${path} -> HTTP ${response.status}`);
  }
  return response.json();
}

export async function fetchSnapshot() {
  return fetchJson(ENDPOINTS.snapshot);
}

export async function fetchOrUnavailable(path) {
  try {
    return await fetchJson(path);
  } catch (error) {
    return { unavailable: String(error) };
  }
}

/** 报告端点：缺失时返回 null（UI 必须显示 UNKNOWN，而不是空报告）。 */
export async function fetchRunSummary() {
  try {
    return await fetchJson(ENDPOINTS.runSummary);
  } catch (error) {
    return { unavailable: String(error) };
  }
}

/** P0001.15 §27：deterministic explain（instrument / venue / reference price / order / decision）。 */
export async function explain(kind, identity) {
  const query = `?kind=${encodeURIComponent(kind)}&identity=${encodeURIComponent(identity || "")}`;
  const payload = await fetchJson(`${ENDPOINTS.assistantExplain}${query}`);
  return payload.explanation || {};
}

/** P0001.15 §15/SC-27：`Decision → Order(s)` 反查（由 canonical correlation 提供）。 */
export async function fetchDecisionOrders(decisionId) {
  return fetchJson(`${ENDPOINTS.decisionOrders}?decision_id=${encodeURIComponent(decisionId)}`);
}
