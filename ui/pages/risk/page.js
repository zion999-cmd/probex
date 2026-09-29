/** Risk 页面（P0001.10.2 §1）：只消费 Product API 切片 `risk`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Risk";
export const slug = "risk";

export async function render() {
  const { risk } = await fetchJson(ENDPOINTS.risk);
  const rejects = (risk.rejects || []).length
    ? rows(risk.rejects.map((r, i) => [`reject ${i + 1}`, `<span class="bad">${escapeHtml(r)}</span>`]))
    : rows([["rejects", "none"]]);
  return section("Risk state", factRows(risk)) + section("Risk rejects (reason codes)", rejects);
}
