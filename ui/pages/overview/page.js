/** Overview 页面（P0001.10.2 §1）：只消费 Product API 切片 `snapshot`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Overview";
export const slug = "overview";

export async function render() {
  const s = await fetchSnapshot();
  const id = s.runtime;
  const orders = s.execution.active_orders || [];
  const banner = rows([
    ["mode / environment", `<span class="tag">${id.mode}</span> <span class="tag">${id.environment}</span>`],
    ["venue / symbol", `${id.venue} / ${id.symbol}`],
    ["runtime_id", escapeHtml(id.runtime_id)],
    ["started_at", String(id.started_at)],
    ["data_timestamp", fact(id.data_timestamp)],
    ["generated_at", String(s.generated_at)],
  ]);
  const headline = rows([
    ["market healthy", fact(s.market.healthy)],
    ["market tradeable", fact(s.market.tradeable)],
    ["prediction fresh", fact(s.prediction.freshest)],
    ["prediction confidence", fact(s.prediction.derived_confidence)],
    ["strategy mode", fact(s.strategy.mode)],
    ["strategy blocked_by", fact(s.strategy.blocked_by)],
    ["risk kill switch", fact(s.risk.kill_switch_mode)],
    ["readiness", fact(s.readiness.status)],
    ["active orders", String(orders.length)],
    ["position qty", fact(s.portfolio.position_qty)],
    ["equity", fact(s.portfolio.equity)],
    ["realized pnl", fact(s.portfolio.realized_pnl)],
  ]);
  return section("Runtime", banner) + section("Headline", headline) +
    section("Readiness blockers", rows((s.evidence.readiness_blockers || []).map((b, i) => [`blocker ${i + 1}`, `<span class="bad">${escapeHtml(b)}</span>`])) || rows([["blockers", "none"]]));
}
