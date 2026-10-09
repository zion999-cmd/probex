/** Monitor：30 秒理解当前系统状态（P0001.12.1 §1）。 */
import { ENDPOINTS, fetchJson, fetchOrUnavailable, fetchSnapshot, reasonCatalog } from "/ui/client/api.js";
import { escapeHtml, fact, positionFact, reasonCell, rows, section } from "/ui/client/render.js";
import { BLOCKER_SECTION, surfaceHash } from "/ui/app/navigation.js";
import { SURFACES } from "/ui/app/surfaces.js";
import { mountSparklines, sparklinesSection } from "/ui/monitor/sparklines.js";

let lastTimeline = null;

export const title = "Monitor";
export const slug = "monitor";

export async function render() {
  const snapshot = await fetchSnapshot();
  const overlays = await fetchJson(ENDPOINTS.marketOverlays);
  const decisions = (overlays.overlays || {}).decisions || [];
  const orders = snapshot.execution.active_orders || [];
  const metrics = rows([
    ["equity", fact(snapshot.portfolio.equity)],
    ["realized pnl", fact(snapshot.portfolio.realized_pnl)],
    ["unrealized pnl", fact(snapshot.portfolio.unrealized_pnl)],
    ["position", positionFact(snapshot.portfolio.position_qty)],
    ["open order exposure", fact(snapshot.execution.open_order_exposure)],
    ["uncertain exposure", fact(snapshot.execution.uncertain_exposure)],
    ["active orders", String(orders.length)],
    ["readiness", fact(snapshot.readiness.status)],
  ]);
  const catalog = await reasonCatalog();
  const blockers = (snapshot.blockers || []).length
    ? (snapshot.blockers || []).map((b) => [b.severity + " · " + b.owner,
        // F-16：Blocker → System 对应 section；F-09：原始 code + 人类解释
        `<a href="${surfaceHash("system", BLOCKER_SECTION[b.owner] || "execution")}">${reasonCell(b.reason_code, catalog)}</a>`])
    : [["blockers", "none"]];
  // P0001.15 §22：instrument / venue / reference price / connector health（两个 connector 分开）
  const instrument = snapshot.instrument || {};
  const venue = snapshot.venue || {};
  const reference = snapshot.reference_price || {};
  const identityRow = rows([
    ["mode / environment", `${fact(snapshot.runtime.mode)} · ${fact(venue.environment)}`],
    ["instrument", `${fact(instrument.instrument_id)} · ${fact(instrument.symbol)}`],
    ["asset class", fact(instrument.asset_class)],
    ["product type", fact(instrument.product_type)],
    ["venue", `${fact(venue.venue_id)} (${fact(venue.environment)})`],
    ["reference price", `${fact(reference.price_type)} ${fact(reference.price)}`],
    ["reference source", fact(reference.source)],
    ["reference freshness (ms)", fact(reference.freshness_ms)],
    ["reference reason", fact(reference.reason)],
  ]);
  const connectorRow = rows([
    ["market connector", `${fact(snapshot.market_connector_health.connector_id)} · ${fact(snapshot.market_connector_health.connection_state)}`],
    ["market event age (ms)", fact(snapshot.market_connector_health.event_age_ms)],
    ["private connector", `${fact(snapshot.private_connector_health.connector_id)} · ${fact(snapshot.private_connector_health.connection_state)}`],
    ["private event age (ms)", fact(snapshot.private_connector_health.event_age_ms)],
  ]);
  const market = rows([
    ["market health", fact(snapshot.market.healthy)],
    ["tradeable", fact(snapshot.market.tradeable)],
    ["best bid / ask", `${fact(snapshot.market.best_bid)} / ${fact(snapshot.market.best_ask)}`],
    ["spread (bps)", fact(snapshot.market.spread_bps)],
    ["prediction fresh", fact(snapshot.prediction.freshest)],
    ["prediction source", predictionSourceLabel(snapshot.prediction)],
    ["strategy mode", fact(snapshot.strategy.mode)],
    ["blocked by", fact(snapshot.strategy.blocked_by)],
    ["kill switch", fact(snapshot.risk.kill_switch_mode)],
  ]);
  const quotes = decisions.length
    ? decisions.slice(-5).map((d) => `<div class="row"><span class="k">${escapeHtml(d.side)}</span>` +
        `<span class="v">${escapeHtml(d.action)} @ ${fact(d.price)}</span></div>`).join("")
    : '<div class="row"><span class="k">quotes</span><span class="v unknown">UNKNOWN (no decisions recorded)</span></div>';
  const orderRows = orders.length
    ? orders.map((o) => `<div class="row"><span class="k">${escapeHtml(o.client_order_id)}</span>` +
        `<span class="v">${escapeHtml(o.side)} ${escapeHtml(o.status)} @ ${fact(o.price)}</span></div>`).join("")
    : '<div class="row"><span class="k">orders</span><span class="v known">0</span></div>';
  const [execHealth, execRate, execReconciliation, timeline] = await Promise.all([
    fetchOrUnavailable(ENDPOINTS.executionHealth), fetchOrUnavailable(ENDPOINTS.executionRateLimits),
    fetchOrUnavailable(ENDPOINTS.executionReconciliation), fetchOrUnavailable(ENDPOINTS.portfolioTimeline),
  ]);
  lastTimeline = timeline;
  const runtimeRow = rows([
    ["runtime state", fact(snapshot.health.runtime_state)],
    ["runtime detail", fact(snapshot.health.runtime_detail)],
    ["quoting", fact(snapshot.health.runtime_quoting)],
    ["run", fact(snapshot.health.runtime_run_id)],
  ]);
  const execSummary = rows([
    ["execution health", execHealth.unavailable
      ? `<span class="unknown">UNKNOWN (${escapeHtml(execHealth.unavailable)})</span>`
      : `<span class="${execHealth.health.status === "HEALTHY" ? "known" : "bad"}">${escapeHtml(execHealth.health.status)}</span>`],
    ["request budget", execRate.unavailable ? '<span class="unknown">UNKNOWN</span>'
      : fact(execRate.governor.request.remaining)],
    ["order budget", execRate.unavailable ? '<span class="unknown">UNKNOWN</span>'
      : fact(execRate.governor.order.remaining)],
    ["uncertain exposure", fact(snapshot.execution.uncertain_exposure)],
    ["reconciliation", execReconciliation.unavailable ? '<span class="unknown">UNKNOWN</span>'
      : fact(execReconciliation.reconciliation.state)],
  ]);
  const recent = decisions.slice(-5).reverse().map((d) =>
    `<div class="row"><span class="k">${d.ts}</span><span class="v">${escapeHtml(d.action)} ${escapeHtml(d.side)} ` +
    `${fact(d.decision_id)}</span></div>`).join("") ||
    '<div class="row"><span class="k">activity</span><span class="v unknown">UNKNOWN (nothing recorded)</span></div>';
  const surfaceNav = SURFACES.map((s) => `<a href="#/${s.slug}">${escapeHtml(s.title)}</a>`).join(" · ");
  // F-12/F-15：四层健康必须可区分（liveness 不暗示可交易）
  const ops = snapshot.ops || {};
  const opsRow = rows([
    ["process live", fact(ops.process_live)],
    ["runtime state", fact(ops.runtime_state)],
    ["trade readiness", fact(ops.trade_readiness)],
    ["execution health", fact(ops.execution_health)],
    ["operational warning", fact(ops.operational_warning)],
  ]);
  return section("Instrument / venue", identityRow) + section("Connectors", connectorRow) +
    section("Process / runtime / readiness / execution", opsRow) +
    section("Runtime / loop", runtimeRow) + section("System now", metrics) +
    `<section class="wide"><h2>Trends (equity / PnL / exposure)</h2>${sparklinesSection()}</section>` +
    section("Execution safety", execSummary) +
    section("Blockers / warnings", rows(blockers)) +
    section("Market / current quotes", market + quotes) +
    section("Position / active orders", orderRows) +
    section("Recent activity", recent) +
    section("Surfaces", surfaceNav);
}

/** P0001.17：LOCAL_TRIAL 必须显式标注（不得描述为真实模型预测）。 */
function predictionSourceLabel(prediction) {
  if (!prediction || !prediction.provider || !prediction.provider.known) {
    return '<span class="unknown">UNKNOWN</span>';
  }
  const provider = escapeHtml(String(prediction.provider.value));
  if (prediction.is_local_trial && prediction.is_local_trial.known && prediction.is_local_trial.value) {
    return `<span class="warn">LOCAL_TRIAL</span> <span class="muted">${provider} · 本地试验规则，非真实模型</span>`;
  }
  return provider;
}

export function mount() {
  mountSparklines(lastTimeline);
}
