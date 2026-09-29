/** Portfolio 页面（P0001.10.2 §1）：只消费 Product API 切片 `portfolio`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Portfolio";
export const slug = "portfolio";

export async function render() {
  const { portfolio } = await fetchJson(ENDPOINTS.portfolio);
  return section("Portfolio", factRows(portfolio));
}
