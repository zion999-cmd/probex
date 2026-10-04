/** Activity：F-08 完整因果链（按时间排序的单一 trace；缺阶段显式 ABSENT + reason）。 */
import { ENDPOINTS, fetchJson, fetchSnapshot, reasonCatalog } from "/ui/client/api.js";
import { escapeHtml, fact, reasonCell, rows, section, table, valueView } from "/ui/client/render.js";
import { entityHash, marketPointHash } from "/ui/app/navigation.js";
import { mountPredictionPanel, predictionSection } from "/ui/pages/market/prediction_panel.js";
import { updateSelection } from "/ui/client/selection.js";

let lastActivity = null;

export const title = "Activity";
export const slug = "activity";

/** 规范阶段顺序（没有事实的阶段必须显式出现，不得静默跳过）。 */
export const CHAIN = ["market_state", "prediction", "maker_decision", "risk", "readiness",
                      "normalization", "order", "ack", "execution_event", "cancel", "fill",
                      "unknown", "reconciliation"];

/** F7：阶段 reason 单元格。
 *
 * - 该阶段在本链路**未产生事实** ⇒ ABSENT（"本次运行未产生该阶段事实"）；
 * - provider/能力不可用 ⇒ UNAVAILABLE；
 * - 有事实但值未知 ⇒ UNKNOWN（保留原始 reason 供高级用户查看）；
 * - 有 reason code ⇒ 走 catalog 解释。
 */
function stageReason(entry, catalog) {
  if (entry.outcome === "absent") {
    const raw = entry.reason_code && !entry.reason_code.known ? String(entry.reason_code.reason || "") : "";
    const unavailable = /not wired|unavailable/i.test(raw);
    const label = unavailable ? "UNAVAILABLE" : "ABSENT";
    const note = unavailable ? "该阶段能力当前不可用" : "本次运行未产生该阶段事实";
    return `<span class="unknown">${label}</span> <span class="muted">${note}</span>` +
      (raw ? `<div class="muted">raw: ${escapeHtml(raw)}</div>` : "");
  }
  if (entry.outcome === "unavailable") return '<span class="unknown">UNAVAILABLE</span>';
  const code = entry.reason_code && entry.reason_code.known ? entry.reason_code.value : null;
  return reasonCell(code, catalog);
}

/** Decision → Order(s)：由 canonical correlation id 反查（P0001.15 §15/§24；不是时间模糊匹配）。 */
function decisionOrderCell(entry) {
  const detail = String(entry.detail || "");
  const match = detail.match(/decision_id=([A-Za-z0-9:_-]+)/);
  if (!match) return '<span class="unknown">UNKNOWN</span>';
  const decisionId = match[1];
  return `<a href="#/orders?decision=${encodeURIComponent(decisionId)}">${escapeHtml(decisionId)}</a>`;
}

function identityCell(entry) {
  const kind = entry.identity_kind || "";
  if (!entry.identity || !entry.identity.known) {
    return `${fact(entry.identity)}${kind ? ` <span class="muted">(${escapeHtml(kind)})</span>` : ""}`;
  }
  const value = String(entry.identity.value);
  if (entry.stage === "order" || entry.stage === "normalization" || entry.stage === "ack") {
    return `<a href="${entityHash("order", value)}">${escapeHtml(value)}</a> <span class="muted">(${escapeHtml(kind)})</span>`;
  }
  if (entry.stage === "fill") {
    return `<a href="${entityHash("fill", value)}">${escapeHtml(value)}</a> <span class="muted">(${escapeHtml(kind)})</span>`;
  }
  if (entry.stage === "execution_event" || entry.stage === "cancel") {
    return `<a href="${entityHash("execution_event", value)}">${escapeHtml(value)}</a> <span class="muted">(${escapeHtml(kind)})</span>`;
  }
  return `${escapeHtml(value)} <span class="muted">(${escapeHtml(kind)})</span>`;
}

export async function render(rest = []) {
  // F-16：Run → Activity 携带 canonical run id（`#/activity/run/<run_id>`）
  const selectedRun = rest[0] === "run" && rest[1] ? String(rest[1]) : null;
  const snapshot = await fetchSnapshot();
  const evidence = await fetchJson(ENDPOINTS.evidence);
  const overlays = await fetchJson(ENDPOINTS.marketOverlays);
  const catalog = await reasonCatalog();
  const trace = (evidence.evidence || {}).trace || [];

  // trace 已由服务端按时间排序；这里再做一次稳定校验（不重排、不丢弃）
  const present = new Set(trace.map((entry) => entry.stage));
  const missing = CHAIN.filter((stage) => !present.has(stage));
  // P0001.15 §24：causal chain 必须带同一 instrument / venue identity
  const timeline = table(["ts", "stage", "outcome", "identity (canonical)", "instrument", "venue",
                          "decision → orders", "reason code", "detail", "market"],
    trace.map((entry) => {
      const ts = entry.ts && entry.ts.known ? String(entry.ts.value) : '<span class="unknown">UNKNOWN</span>';
      const market = entry.ts && entry.ts.known
        ? `<a href="${marketPointHash(entry.ts.value)}">market @ ${escapeHtml(String(entry.ts.value))}</a>`
        : '<span class="unknown">no timestamp</span>';
      const latency = entry.latency_ms && entry.latency_ms.known
        ? ` <span class="muted">latency=${escapeHtml(String(entry.latency_ms.value))}ms</span>` : "";
      return [ts, escapeHtml(entry.stage), escapeHtml(entry.outcome),
        identityCell(entry), fact(entry.instrument_id), fact(entry.venue_id),
        decisionOrderCell(entry), stageReason(entry, catalog),
        `${escapeHtml(entry.detail || "")}${latency}`, market];
    }));
  const missingBlock = missing.length
    ? rows(missing.map((stage) => [stage,
        '<span class="unknown">ABSENT</span> <span class="muted">本次运行未产生该阶段事实</span>']))
    : rows([["stages", "每个 canonical 阶段都有事实或显式 ABSENT entry"]]);

  const decisions = ((overlays.overlays || {}).decisions || []).slice(-10).reverse();
  const executions = ((overlays.overlays || {}).executions || []).slice(-10).reverse();
  const flow = rows([
    ["prediction freshness", fact(snapshot.prediction.freshest)],
    ["prediction confidence", fact(snapshot.prediction.derived_confidence)],
    ["strategy blocked_by", fact(snapshot.strategy.blocked_by)],
    ["risk rejects", escapeHtml((snapshot.risk.rejects || []).join(", ") || "none")],
    ["readiness", fact(snapshot.readiness.status)],
    ["readiness blockers", escapeHtml((snapshot.readiness.reasons || []).join(", ") || "none")],
  ]);
  const decisionTable = table(["ts", "side", "action", "price", "qty", "decision id", "reason"],
    decisions.map((d) => [String(d.ts), escapeHtml(d.side), escapeHtml(d.action), fact(d.price),
      fact(d.quantity), fact(d.decision_id), fact(d.reason)]));
  const executionTable = table(["ts", "order", "event", "detail"],
    executions.map((e) => [String(e.ts), escapeHtml(e.client_order_id), escapeHtml(e.event),
      escapeHtml(e.detail || "")]));
  const strategy = snapshot.strategy || {};
  const bid = rows([["bid action", fact(strategy.bid_action)], ["bid price", fact(strategy.bid_price)],
                    ["bid qty", fact(strategy.bid_quantity)]]);
  const ask = rows([["ask action", fact(strategy.ask_action)], ["ask price", fact(strategy.ask_price)],
                    ["ask qty", fact(strategy.ask_quantity)]]);
  const strategyPanel = section("Strategy / Decision（为什么交易 / 为什么不交易 / 为什么取消）", rows([
    ["strategy identity", "MakerPolicy"] ,
    ["decision at_ms", fact(strategy.at_ms)],
    ["mode", fact(strategy.mode)],
    ["signal / blocked_by", fact(strategy.blocked_by)],
    ["detail", fact(strategy.detail)],
    ["risk result", escapeHtml((snapshot.risk.rejects || []).join(", ") || "no rejects")],
    ["readiness result", `${fact(snapshot.readiness.status)} ${escapeHtml((snapshot.readiness.reasons || []).join(", "))}`],
    ["prediction linkage", fact(snapshot.prediction.request_id)],
  ])) + section("MakerPolicy bid / ask", bid + ask) +
    `<section class="wide"><div id="activity-prediction"></div></section>`;
  lastActivity = snapshot;
  const runLinks = rows([
    ["selected run", selectedRun
      ? `<code>${escapeHtml(selectedRun)}</code> · ` +
        `<a href="#/market/run-review/${encodeURIComponent(selectedRun)}">Run Review</a>`
      : "none (open a run from Performance → Runs)"],
    ["trace scope", "the trace is the live session causal chain (per-run persisted traces are not in Slice 4)"],
    ["Run → Run Review", `<a href="#/market/run-review">Market / Run Review</a>`],
  ]);
  return section("Causal chain (time-ordered, F-08)", timeline) +
    section("Stages without a fact (explicit, not skipped)", missingBlock) +
    strategyPanel +
    section("Inputs / gates", flow) +
    section("Decisions", decisionTable) +
    section("Exchange events", executionTable) +
    section("Drill-down", runLinks) +
    section("Execution drill-down (P0001.13)", rows([
      ["chain", "market → prediction → decision → risk → readiness → normalization → submit → ack → events → fill/cancel/unknown → reconciliation"],
      ["normalization", "execution-boundary evidence (input → normalized, rounding, reject reason)"],
      ["execution health", valueView(snapshot.execution_safety.health_status)],
      ["reconciliation", valueView(snapshot.execution_safety.reconciliation)],
    ])) +
    section("Raw facts drill-down (G2)", rows([
      ["order / fill", "click an identity above → Evidence → raw facts"],
      ["raw facts endpoint", "<code>/api/v1/facts/&lt;kind&gt;/&lt;identity&gt;</code> " +
        "(kind: order | fill | decision | execution_event | prediction)"],
    ]));
}

export function mount() {
  updateSelection({ surface: "activity" });
  const snapshot = lastActivity;
  const host = document.getElementById("activity-prediction");
  if (host && snapshot) {
    host.innerHTML = predictionSection(snapshot.prediction, { id: "activity-prediction-chart" });
    mountPredictionPanel(document.getElementById("activity-prediction-chart"), snapshot.prediction);
  }
}
