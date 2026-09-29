/** Strategy 页面（P0001.10.2 §1）：只消费 Product API 切片 `strategy`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Strategy";
export const slug = "strategy";

export async function render() {
  const { strategy } = await fetchJson(ENDPOINTS.strategy);
  return section("Strategy decision", factRows(strategy));
}
