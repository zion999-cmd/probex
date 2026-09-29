/** Runs（P0001.11 §2）：run 列表 + 单个 run 的 summary/metrics + 两个 run 对比（UNKNOWN 不填 0）。 */
import { ENDPOINTS, fetchOrUnavailable } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Runs";
export const slug = "runs";

export async function render() {
  const runsPayload = await fetchOrUnavailable(ENDPOINTS.runs);
  if (runsPayload.unavailable) {
    return section("Run registry", rows([["registry",
      `<span class="unknown">UNKNOWN (${escapeHtml(runsPayload.unavailable)})</span>`]]));
  }
  const runs = runsPayload.runs || [];
  const list = table(["run_id", "status", "started_at", "config_id", "fingerprint"],
    runs.map((run) => [escapeHtml(run.run_id || ""), escapeHtml(run.status || ""),
      String(run.started_at ?? ""), fact(run.config_id), fact(run.config_fingerprint)]));
  const summary = await fetchOrUnavailable(ENDPOINTS.runSummary);
  const summaryBlock = summary.unavailable
    ? rows([["run summary", `<span class="unknown">UNKNOWN (${escapeHtml(summary.unavailable)})</span>`]])
    : factRows(summary.run_summary || {});
  const metrics = summary.unavailable ? "" : section("Metrics (from the run summary)",
    factRows(summary.run_summary.metrics || {}));
  let compare = rows([["compare", "provide two run ids via the API (left/right)"]]) ;
  if (runs.length >= 2) {
    const comparison = await fetchOrUnavailable(
      `${ENDPOINTS.runCompare}?left=${encodeURIComponent(runs[0].run_id)}&right=${encodeURIComponent(runs[1].run_id)}`);
    if (!comparison.unavailable) {
      const data = comparison.comparison || {};
      compare = table(["metric", "left", "right", "delta"],
        (data.metrics || []).map((metric) => [escapeHtml(metric.name), fact(metric.left), fact(metric.right),
          fact(metric.delta)]));
    }
  }
  return section("Runs", list) + section("Latest run summary", summaryBlock) + metrics +
    section(`Compare (${runs.length >= 2 ? escapeHtml(runs[0].run_id) + " vs " + escapeHtml(runs[1].run_id) : "n/a"})`,
      compare);
}
