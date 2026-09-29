/** Activity：F-08 完整因果链（按时间排序的单一 trace；缺阶段显式 ABSENT + reason）。 */
import { ENDPOINTS, fetchJson, fetchSnapshot, reasonCatalog } from "/ui/client/api.js";
import { escapeHtml, fact, reasonCell, rows, section, table } from "/ui/client/render.js";
import { entityHash, marketPointHash } from "/ui/app/navigation.js";

export const title = "Activity";
export const slug = "activity";

/** 规范阶段顺序（没有事实的阶段必须显式出现，不得静默跳过）。 */
export const CHAIN = ["market_state", "prediction", "maker_decision", "risk", "readiness",
                      "normalization", "order", "ack", "execution_event", "cancel", "fill",
                      "unknown", "reconciliation"];

function identityCell(entry) {
  const kind = entry.identity_kind || "";
  if (!entry.identity || !entry.identity.known) {
    return `${fact(entry.identity)}${kind ? ` <span class="muted">(${escapeHtml(kind)})</span>` : ""}`;
  }
  const value = String(entry.identity.value);
  if (entry.stage === "order" || entry.stage === "normalization" || entry.stage === "ack") {
    return `<a href="${entityHash("order", value)}">${escapeHtml(value)}</a> <span class="muted">(${escapeHtml(kind)})</span>`;
  }
  if (entry.stage === "fill") {
    return `<a href="${entityHash("fill", value)}">${escapeHtml(value)}</a> <span class="muted">(${escapeHtml(kind)})</span>`;
  }
  if (entry.stage === "execution_event" || entry.stage === "cancel") {
    return `<a href="${entityHash("execution_event", value)}">${escapeHtml(value)}</a> <span class="muted">(${escapeHtml(kind)})</span>`;
  }
  return `${escapeHtml(value)} <span class="muted">(${escapeHtml(kind)})</span>`;
}

export async function render() {
  const snapshot = await fetchSnapshot();
  const evidence = await fetchJson(ENDPOINTS.evidence);
  const overlays = await fetchJson(ENDPOINTS.marketOverlays);
  const catalog = await reasonCatalog();
  const trace = (evidence.evidence || {}).trace || [];

  // trace 已由服务端按时间排序；这里再做一次稳定校验（不重排、不丢弃）
  const present = new Set(trace.map((entry) => entry.stage));
  const missing = CHAIN.filter((stage) => !present.has(stage));
  const timeline = table(["ts", "stage", "outcome", "identity (canonical)", "reason code", "detail", "market"],
    trace.map((entry) => {
      const ts = entry.ts && entry.ts.known ? String(entry.ts.value) : '<span class="unknown">UNKNOWN</span>';
      const market = entry.ts && entry.ts.known
        ? `<a href="${marketPointHash(entry.ts.value)}">market @ ${escapeHtml(String(entry.ts.value))}</a>`
        : '<span class="unknown">no timestamp</span>';
      const latency = entry.latency_ms && entry.latency_ms.known
        ? ` <span class="muted">latency=${escapeHtml(String(entry.latency_ms.value))}ms</span>` : "";
      return [ts, escapeHtml(entry.stage), escapeHtml(entry.outcome),
        identityCell(entry), reasonCell(entry.reason_code && entry.reason_code.known
          ? entry.reason_code.value : null, catalog),
        `${escapeHtml(entry.detail || "")}${latency}`, market];
    }));
  const missingBlock = missing.length
    ? rows(missing.map((stage) => [stage, '<span class="unknown">ABSENT (no fact at this stage)</span>']))
    : rows([["stages", "all canonical stages have a fact or an explicit absent entry"]]);

  const decisions = ((overlays.overlays || {}).decisions || []).slice(-10).reverse();
  const executions = ((overlays.overlays || {}).executions || []).slice(-10).reverse();
  const flow = rows([
    ["prediction freshness", fact(snapshot.prediction.freshest)],
    ["prediction confidence", fact(snapshot.prediction.derived_confidence)],
    ["strategy blocked_by", fact(snapshot.strategy.blocked_by)],
    ["risk rejects", escapeHtml((snapshot.risk.rejects || []).join(", ") || "none")],
    ["readiness", fact(snapshot.readiness.status)],
    ["readiness blockers", escapeHtml((snapshot.readiness.reasons || []).join(", ") || "none")],
  ]);
  const decisionTable = table(["ts", "side", "action", "price", "qty", "decision id", "reason"],
    decisions.map((d) => [String(d.ts), escapeHtml(d.side), escapeHtml(d.action), fact(d.price),
      fact(d.quantity), fact(d.decision_id), fact(d.reason)]));
  const executionTable = table(["ts", "order", "event", "detail"],
    executions.map((e) => [String(e.ts), escapeHtml(e.client_order_id), escapeHtml(e.event),
      escapeHtml(e.detail || "")]));
  const runLinks = rows([
    ["Run → Activity", "open a run in Performance, then drill into this trace"],
    ["Run → Run Review", `<a href="#/market/run-review">Market / Run Review</a>`],
  ]);
  return section("Causal chain (time-ordered, F-08)", timeline) +
    section("Stages without a fact (explicit, not skipped)", missingBlock) +
    section("Inputs / gates", flow) +
    section("Decisions", decisionTable) +
    section("Exchange events", executionTable) +
    section("Drill-down", runLinks) +
    section("Execution drill-down (P0001.13)", rows([
      ["chain", "market → prediction → decision → risk → readiness → normalization → submit → ack → events → fill/cancel/unknown → reconciliation"],
      ["normalization", "execution-boundary evidence (input → normalized, rounding, reject reason)"],
      ["execution health", fact(snapshot.execution_safety.health_status)],
      ["reconciliation", fact(snapshot.execution_safety.reconciliation)],
    ])) +
    section("Raw facts drill-down (G2)", rows([
      ["order / fill", "click an identity above → Evidence → raw facts"],
      ["raw facts endpoint", "<code>/api/v1/facts/&lt;kind&gt;/&lt;identity&gt;</code> " +
        "(kind: order | fill | decision | execution_event | prediction)"],
    ]));
}
