/** Runs 页面（P0001.10.2 §1）：只消费 Product API 切片 `run-summary`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Runs";
export const slug = "runs";

export async function render() {
  const summary = await fetchRunSummary();
  if (summary.unavailable) {
    return section("Run summary", rows([["report", `<span class="unknown">UNKNOWN (${escapeHtml(summary.unavailable)})</span>`],
      ["markdown", `<a href="${ENDPOINTS.runSummary}?format=markdown">download</a>`]]));
  }
  return section("Run identity", factRows(summary.run_summary.run)) +
    section("Counts", rows(Object.entries(summary.run_summary.decision_counts || {}).map(([k, v]) => [k, String(v)])) +
      rows(Object.entries(summary.run_summary.order_counts || {}).map(([k, v]) => [`order ${k}`, String(v)]))) +
    section("Facts", factRows({
      fills: summary.run_summary.fills, fees: summary.run_summary.fees,
      realized_pnl: summary.run_summary.realized_pnl, unrealized_pnl: summary.run_summary.unrealized_pnl,
      max_exposure: summary.run_summary.max_exposure, final_position: summary.run_summary.final_position,
    })) +
    section("Anomalies", rows((summary.run_summary.anomalies || []).map((a, i) => [`anomaly ${i + 1}`, `<span class="bad">${escapeHtml(a)}</span>`])) ||
      rows([["anomalies", "none"]]));
}
