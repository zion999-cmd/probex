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
  replay: "/api/v1/replay",
};

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
