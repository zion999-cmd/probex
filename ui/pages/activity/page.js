/** Activity：F-08 完整因果链（按时间排序的单一 trace；缺阶段显式 ABSENT + reason）。 */
import { ENDPOINTS, fetchDecisionDetail, fetchJson, fetchSnapshot, reasonCatalog } from "/ui/client/api.js";
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

/** decision id → 可点击的 drill-down（拉取 /api/v1/decisions/detail）。 */
function decisionLink(decisionFact) {
  if (!decisionFact || !decisionFact.known || !decisionFact.value) return fact(decisionFact);
  const value = String(decisionFact.value);
  return `<a href="#" data-decision="${escapeHtml(value)}">${escapeHtml(value)}</a>`;
}

async function mountDecisionDrilldown(host) {
  const panel = document.getElementById("decision-detail");
  if (!panel) return;
  // decision 链接位于 "Decisions" 表（不在 drill-down 区域内）⇒ 必须在整个文档范围内绑定
  (host || document).querySelectorAll("a[data-decision]").forEach((link) => {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      const decisionId = link.dataset.decision;
      updateSelection({ surface: "activity", run: (lastActivity || {}).runtime
        ? lastActivity.runtime.runtime_id : null, decision: decisionId, order: null, fill: null });
      panel.innerHTML = `<div class="unknown">loading ${escapeHtml(decisionId)}…</div>`;
      try {
        const payload = await fetchDecisionDetail(decisionId);
        panel.innerHTML = renderDecisionDetail(payload.detail);
      } catch (error) {
        panel.innerHTML = `<div class="bad">ERROR: ${escapeHtml(String(error))} ` +
          `<div class="muted">恢复：确认该 decision 是否属于当前运行（历史 run 的因果链未持久化），` +
          `或刷新后从 Activity 的 trace 重新选择。</div></div>`;
      }
    });
  });
}

/** 因果链渲染：market → prediction → decision → risk → order → fill → accounting（缺项如实 UNKNOWN）。 */
function renderDecisionDetail(detail) {
  if (!detail) return '<div class="unknown">UNKNOWN (no detail)</div>';
  const decision = detail.decision;
  const prediction = detail.prediction;
  const risk = (detail.risk || []).map((r) =>
    `${escapeHtml(r.client_order_id)}: ${escapeHtml(r.decision)}${r.reason_code ? " (" + escapeHtml(r.reason_code) + ")" : ""}`);
  const orders = (detail.orders || []).map((o) =>
    `<a href="#/activity" data-order="${escapeHtml(o.client_order_id)}">${escapeHtml(o.client_order_id)}</a>` +
    ` · ${escapeHtml(o.side)} · ${escapeHtml(o.status)} · filled=${fact(o.filled_quantity)}` +
    ` · venue_order_id=${fact(o.venue_order_id)}` +
    ` · <a href="${marketPointHash(o.created_at)}">market @ ${o.created_at}</a>`);
  const fills = (detail.fills || []).map((f) =>
    `<a href="#/activity" data-order="${escapeHtml(f.client_order_id)}" data-fill="${escapeHtml(String(f.trade_id))}">` +
    `${escapeHtml(f.client_order_id)}</a> · price=${fact(f.price)} · qty=${fact(f.quantity)} · fee=${fact(f.fee)}` +
    (f.ts ? ` · <a href="${marketPointHash(f.ts)}">market @ ${f.ts}</a>` : ""));
  const predictionLine = prediction
    ? `${escapeHtml(String(prediction.provider))}${prediction.is_local_trial ? ' <span class="warn">LOCAL_TRIAL</span>' : ""}` +
      ` · model=${escapeHtml(String(prediction.model))} · confidence=${prediction.derived_confidence === null ? "UNKNOWN" : escapeHtml(String(prediction.derived_confidence))}` +
      ` · request=${escapeHtml(String(prediction.request_id))}`
    : '<span class="unknown">UNKNOWN（该 decision 无 prediction 记录）</span>';
  return section(`Decision ${escapeHtml(detail.decision_id)}`, rows([
    ["instrument / venue", `${escapeHtml(String(detail.instrument_id))} · ${escapeHtml(String(detail.venue_id))}`],
    ["market state hash", escapeHtml(String(detail.market_state_hash || "UNKNOWN"))],
    ["prediction", predictionLine],
    ["decision", decision === null
      ? '<span class="unknown">UNKNOWN（该 decision 不是当前最新一轮；订单/成交仍可反查）</span>'
      : `${escapeHtml(String(decision.mode))} @ ${escapeHtml(String(decision.at_ms))} · bid=${escapeHtml(String(decision.bid_action))}@${decision.bid_price === null ? "UNKNOWN" : escapeHtml(String(decision.bid_price))}` +
        ` · ask=${escapeHtml(String(decision.ask_action))}@${decision.ask_price === null ? "UNKNOWN" : escapeHtml(String(decision.ask_price))}`],
    ["decision reason", decision === null ? "UNKNOWN" : escapeHtml(String(decision.reason || "—"))],
    ["risk", risk.length ? risk.join("<br>") : '<span class="unknown">UNKNOWN（无 risk 判定记录）</span>'],
    ["orders", orders.length ? orders.join("<br>") : '<span class="unknown">UNKNOWN（该 decision 未产生订单）</span>'],
    ["fills", fills.length ? fills.join("<br>") : '<span class="unknown">UNKNOWN（该 decision 尚无成交）</span>'],
    ["accounting", detail.accounting === null ? '<span class="unknown">UNKNOWN</span>'
      : `position=${detail.accounting.position_qty === null ? "UNKNOWN" : escapeHtml(String(detail.accounting.position_qty))}` +
        ` · equity=${detail.accounting.equity === null ? "UNKNOWN" : escapeHtml(String(detail.accounting.equity))}` +
        ` · realized=${detail.accounting.realized_pnl === null ? "UNKNOWN" : escapeHtml(String(detail.accounting.realized_pnl))}`],
  ]));
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
  // P0001.17 §7：decision → order → fill → accounting 的 drill-down（点击 decision id）
  const decisionTable = table(["ts", "side", "action", "price", "qty", "decision id", "reason"],
    decisions.map((d) => [String(d.ts), escapeHtml(d.side), escapeHtml(d.action), fact(d.price),
      fact(d.quantity), decisionLink(d.decision_id), fact(d.reason)]));
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
    `<section class="wide"><h2>Decision drill-down</h2>` +
      `<div id="decision-detail"><div class="muted">click a decision id below to expand ` +
      `market → prediction → decision → risk → order → fill → accounting</div></div></section>` +
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
  const drillHost = document.getElementById("decision-detail");
  if (drillHost) {
    void mountDecisionDrilldown(document);          // 文档级绑定（链接在 Decisions 表里）
    // 首次进入自动展开最近一条 decision（真实记录里存在的 decision id）
    const first = document.querySelector("a[data-decision]");
    if (first) first.click();
  }
  const snapshot = lastActivity;
  const host = document.getElementById("activity-prediction");
  if (host && snapshot) {
    host.innerHTML = predictionSection(snapshot.prediction, { id: "activity-prediction-chart" });
    mountPredictionPanel(document.getElementById("activity-prediction-chart"), snapshot.prediction);
  }
}
