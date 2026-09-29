/** Prediction 页面（P0001.10.2 §1）：只消费 Product API 切片 `prediction`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Prediction";
export const slug = "prediction";

export async function render() {
  const { prediction } = await fetchJson(ENDPOINTS.prediction);
  return section("Prediction", factRows(prediction));
}
