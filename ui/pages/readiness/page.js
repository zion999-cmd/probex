/** Readiness 页面（P0001.10.2 §1）：只消费 Product API 切片 `readiness`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Readiness";
export const slug = "readiness";

export async function render() {
  const { readiness } = await fetchJson(ENDPOINTS.readiness);
  const blockers = (readiness.reasons || []).length
    ? rows(readiness.reasons.map((r, i) => [`reason ${i + 1}`, `<span class="bad">${escapeHtml(r)}</span>`]))
    : rows([["reasons", "none"]]);
  const details = (readiness.details || []).length
    ? `<pre>${escapeHtml((readiness.details || []).join("\n"))}</pre>` : rows([["details", "none"]]);
  return section("Readiness", factRows(readiness)) + section("Blockers", blockers) + section("Details", details);
}
