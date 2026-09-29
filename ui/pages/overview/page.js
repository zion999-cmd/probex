/** Overview（P0001.11）：runtime identity + headline + **统一 blockers** + config provenance。 */
import { fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section } from "/ui/client/render.js";

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
    ["strategy mode", fact(s.strategy.mode)],
    ["strategy blocked_by", fact(s.strategy.blocked_by)],
    ["risk kill switch", fact(s.risk.kill_switch_mode)],
    ["readiness", fact(s.readiness.status)],
    ["active orders", String(orders.length)],
    ["position qty", fact(s.portfolio.position_qty)],
    ["equity", fact(s.portfolio.equity)],
    ["realized pnl", fact(s.portfolio.realized_pnl)],
  ]);
  const blockers = (s.blockers || []);
  const blockerRows = blockers.length
    ? blockers.map((b, i) => [`${i + 1}. ${b.severity} · ${b.owner}`,
        `<span class="${b.severity === "BLOCKING" ? "bad" : "unknown"}">${escapeHtml(b.reason_code)}</span> ` +
        `<span class="k">${escapeHtml(b.source_ref)}</span> ${escapeHtml(b.message || "")}`])
    : [["blockers", "none"]];
  const config = s.config || {};
  return banner ? section("Runtime", banner) : "" +
    section("Headline", headline) +
    section("Blockers (unified)", rows(blockerRows)) +
    section("Config provenance", rows([
      ["config_id", fact(config.config_id)],
      ["fingerprint", fact(config.fingerprint)],
      ["created_at", fact(config.created_at)],
      ["sources", escapeHtml(JSON.stringify(config.sources || {}))],
      ["secret refs", escapeHtml((config.secret_refs || []).join(", ") || "none")],
    ]));
}
