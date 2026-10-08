/** Orders 页面（P0001.10.2 §1 + P0001.15 §25）：只消费 Product API 切片 `orders`。
 *
 * P0001.15 §15/§25：每张订单都显示 canonical correlation（decision / instrument / venue / prediction），
 * 且能双向跳转（Order → Decision → 同 decision 的其他订单）。
 */
import { ENDPOINTS, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, rows, section, table } from "/ui/client/render.js";
import { entityHash, marketPointHash } from "/ui/app/navigation.js";

let lastSnapshot = null;

export const title = "Orders";

/** instrument / venue / connector 事实来自 snapshot（订单只带 correlation id，不重复事实）。 */
function instrumentProductType() {
  const instrument = (lastSnapshot && lastSnapshot.instrument) || {};
  return `${fact(instrument.asset_class)} · ${fact(instrument.product_type)}`;
}

function connectorId(kind) {
  const health = kind === "private" ? (lastSnapshot && lastSnapshot.private_connector_health) : null;
  return fact((health || {}).connector_id);
}

/** Order → Decision：跳到 Activity 的同一决策（decision id 可点击，双向可查）。 */
function decisionCell(decisionId) {
  if (!decisionId || !decisionId.known) return fact(decisionId);
  return `<a href="#/activity?decision=${encodeURIComponent(decisionId.value)}">${escapeHtml(decisionId.value)}</a>`;
}
export const slug = "orders";

export async function render() {
  lastSnapshot = await fetchSnapshot();
  const { execution } = await fetchJson(ENDPOINTS.orders);
  const orders = execution.active_orders || [];
  const table_ = table(["client id", "side", "status", "price", "qty", "filled",
                        "decision", "instrument", "venue"],
    orders.map((o) => [escapeHtml(o.client_order_id), escapeHtml(o.side),
      escapeHtml(o.status), fact(o.price), fact(o.quantity), fact(o.filled_quantity),
      decisionCell(o.decision_id), fact(o.instrument_id), fact(o.venue_id)]));
  const detail = orders.length
    ? orders.slice(0, 3).map((o) => section(`Order ${escapeHtml(o.client_order_id)}`, rows([
        ["instrument", fact(o.instrument_id)],
        ["product type", instrumentProductType()],
        ["venue", fact(o.venue_id)],
        ["execution connector", connectorId("private")],
        ["decision id", decisionCell(o.decision_id)],
        ["client order id", escapeHtml(o.client_order_id)],
        ["venue order id", fact(o.venue_order_id)],
        ["prediction id", fact(o.prediction_id)],
        ["last private event", fact((lastSnapshot.private_connector_health || {}).last_event_ms)],
        ["connector state", fact((lastSnapshot.private_connector_health || {}).connection_state)],
        ["created / updated", `${o.created_at} / ${o.updated_at}`],
        // §25：Order → Decision / Order → Evidence / Order → Market timestamp
        ["jump to", `<a href="${entityHash("order", o.client_order_id)}">evidence</a>` +
          ` · <a href="${marketPointHash(o.created_at)}">market @ ${o.created_at}</a>` +
          ` · ${decisionCell(o.decision_id)}`],
      ]))).join("")
    : "";
  return section("Active orders", table_) + detail + section("Exposure", rows([
    ["uncertain exposure", fact(execution.uncertain_exposure)],
    ["open order exposure", fact(execution.open_order_exposure)],
    ["has unknown exposure", `<span class="${execution.has_unknown_exposure ? "bad" : "known"}">${execution.has_unknown_exposure}</span>`],
  ]));
}
