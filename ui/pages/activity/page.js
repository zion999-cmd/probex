/** Activity：因果时间线（Market → Prediction → Decision → Risk → Readiness → Order → Exchange）（§3）。 */
import { ENDPOINTS, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, rows, section, table } from "/ui/client/render.js";

export const title = "Activity";
export const slug = "activity";

const CHAIN = ["market_state", "prediction", "maker_decision", "readiness", "order", "execution_event"];

export async function render() {
  const snapshot = await fetchSnapshot();
  const evidence = await fetchJson(ENDPOINTS.evidence);
  const overlays = await fetchJson(ENDPOINTS.marketOverlays);
  const trace = (evidence.evidence || {}).trace || [];
  const ordered = CHAIN.map((stage) => [stage, trace.filter((entry) => entry.stage === stage)]);

  const timeline = table(["stage", "identity", "outcome", "reason code", "detail"],
    ordered.flatMap(([stage, entries]) => entries.length
      ? entries.map((entry) => [escapeHtml(stage), fact(entry.identity), escapeHtml(entry.outcome),
          fact(entry.reason_code), escapeHtml(entry.detail || "")])
      : [[escapeHtml(stage), '<span class="unknown">UNKNOWN</span>', "not recorded",
          '<span class="unknown">UNKNOWN</span>', ""]]));

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
  return section("Causal chain", timeline) +
    section("Inputs / gates", flow) +
    section("Decisions", decisionTable) +
    section("Exchange events", executionTable) +
    section("Evidence is a detail view", rows([
      ["drill-down", "Activity → detail → Evidence → raw facts"],
      ["raw facts", '<a href="#/system">System / Configuration</a>']]));
}
