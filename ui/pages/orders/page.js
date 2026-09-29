/** Orders 页面（P0001.10.2 §1）：只消费 Product API 切片 `orders`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Orders";
export const slug = "orders";

export async function render() {
  const { execution } = await fetchJson(ENDPOINTS.orders);
  const table_ = table(["client id", "side", "status", "price", "qty", "filled", "decision"],
    (execution.active_orders || []).map((o) => [escapeHtml(o.client_order_id), escapeHtml(o.side),
      escapeHtml(o.status), fact(o.price), fact(o.quantity), fact(o.filled_quantity), fact(o.decision_id)]));
  return section("Active orders", table_) + section("Exposure", rows([
    ["uncertain exposure", fact(execution.uncertain_exposure)],
    ["open order exposure", fact(execution.open_order_exposure)],
    ["has unknown exposure", `<span class="${execution.has_unknown_exposure ? "bad" : "known"}">${execution.has_unknown_exposure}</span>`],
  ]));
}
