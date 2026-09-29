/** Metrics（P0001.11 §3）：只显示 Metric Contract 定义（UI **不**自己计算指标）。 */
import { ENDPOINTS, fetchJson } from "/ui/client/api.js";
import { escapeHtml, section, table } from "/ui/client/render.js";

export const title = "Metrics";
export const slug = "metrics";

export async function render() {
  const payload = await fetchJson(ENDPOINTS.metrics);
  const definitions = payload.definitions || [];
  return section("Metric contract (definitions only)", table(
    ["metric", "formula", "time base", "fact owner", "UNKNOWN condition", "sampling"],
    definitions.map((d) => [escapeHtml(d.name), escapeHtml(d.formula), escapeHtml(d.time_base),
      escapeHtml(d.fact_owner), escapeHtml(d.unknown_condition), escapeHtml(d.sampling)])));
}
