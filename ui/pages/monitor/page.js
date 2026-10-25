/**
 * Monitor（P0018 IA 重构）——只回答四件事：
 *   1. 系统现在是否正常？  2. 当前运行什么？
 *   3. 资金/仓位/风险怎样？  4. 是否需要人工处理？
 *
 * 渲染契约（P0018 §8）：
 * - render() 只在 route 进入时构建一次稳定 DOM（card/container 带固定 id/data-bind），
 *   首帧即填入真实值（SSR/Node 渲染同样成立，不靠 microtask）；
 * - refresh() 只 patch 数据节点的 textContent/className 与列表容器内容，
 *   绝不整体替换 workspace DOM（scroll/展开/输入态因此天然保留）。
 *
 * 原始 blocker/event 行不在本页铺出（全局条带只给聚合摘要）：
 * 原因/下一步/证据一律去 Activity → Evidence；工程细节收进折叠的 diagnostics。
 */
import { ENDPOINTS, fetchOrUnavailable, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact } from "/ui/client/render.js";
import { summarizeBlockers } from "/ui/client/console.js";
import { sparklinesSection } from "/ui/monitor/sparklines.js";
import { invoke } from "/ui/assistant/actions.js";

export const title = "Monitor";
export const slug = "monitor";

let lastTimeline = null;

// ------------------------------------------------------------------ 模型计算

function healthClass(status) {
  const value = String(status || "").toUpperCase();
  if (value === "HEALTHY" || value === "NORMAL" || value === "RUNNING" || value === "ACTIVE" || value === "both") return "known";
  if (value === "UNKNOWN" || value.includes("UNAVAILABLE")) return "unknown";
  return "bad";
}

async function gatherModel() {
  const snapshot = await fetchSnapshot();
  let execHealth = { unavailable: null };
  try { execHealth = await fetchOrUnavailable(ENDPOINTS.executionHealth); } catch { /* 保留 UNKNOWN */ }
  const execStatus = execHealth.unavailable ? "UNKNOWN"
    : String(execHealth.health?.status ?? "UNKNOWN");
  let manifest = { unavailable: "fetch-failed" };
  try { manifest = await fetchOrUnavailable(ENDPOINTS.actions); } catch { /* reconcile 不渲染 */ }
  let decisions = [];
  try {
    const overlays = await fetchJson(ENDPOINTS.marketOverlays);
    decisions = overlays.overlays?.decisions || [];
  } catch { decisions = []; }
  const blockers = Array.isArray(snapshot.blockers) ? snapshot.blockers : [];
  return { snapshot, execStatus, manifest, decisions, summary: summarizeBlockers(blockers) };
}

/** 纯计算：snapshot + execStatus → 各 data-bind 节点的文本/类（render 与 refresh 共用）。 */
function bindValues(model) {
  const { snapshot, execStatus } = model;
  const id = snapshot.runtime;
  const pos = snapshot.portfolio?.position_qty;
  const positionText = pos?.known ? (Number(pos.value) === 0 ? "flat (0)" : compact(pos.value)) : "UNKNOWN";
  const marketHealthy = snapshot.market?.healthy;
  const marketLabel = marketHealthy?.known
    ? (marketHealthy.value ? "HEALTHY" : "UNHEALTHY") +
      (snapshot.market?.tradeable?.known ? (snapshot.market.tradeable.value ? " · tradeable" : " · not tradeable") : "")
    : "UNKNOWN";
  const strategyMode = snapshot.strategy?.mode;
  const readiness = snapshot.readiness?.status;
  return {
    mode: { text: String(id.mode ?? "UNKNOWN"), cls: "" },
    instrument: { text: String(id.symbol ?? "UNKNOWN"), cls: "" },
    runtime_state: { text: `${String(snapshot.health?.runtime_state?.value ?? "runtime UNKNOWN")} · ` +
        `${String(id.venue ?? "")} · ${String(id.environment ?? "")}`, cls: "muted" },
    overall_health: { text: model.summary.state, cls: "health-pill " + model.summary.state.toLowerCase() },
    equity: factModel(snapshot.portfolio?.equity),
    realized_pnl: factModel(snapshot.portfolio?.realized_pnl),
    unrealized_pnl: factModel(snapshot.portfolio?.unrealized_pnl),
    position: { text: positionText, cls: pos?.known ? "known" : "unknown",
                 title: pos?.known ? String(pos.value ?? "") : String(pos?.reason || "unknown") },
    exposure: factModel(snapshot.execution?.open_order_exposure),
    market_health: { text: marketLabel, cls: "chip-value " + healthClass(marketLabel) },
    strategy_state: { text: strategyMode?.known ? String(strategyMode.value) : "UNKNOWN",
                       cls: "chip-value " + (strategyMode?.known ? "known" : "unknown") },
    execution_health: { text: execStatus, cls: "chip-value " + healthClass(execStatus) },
    readiness: { text: readiness?.known ? String(readiness.value) : "UNKNOWN",
                  cls: "chip-value " + healthClass(readiness?.value) },
    attention: { text: model.summary.text, cls: "", state: model.summary.state },
  };
}

function factModel(factValue) {
  if (factValue && typeof factValue === "object" && "known" in factValue) {
    return {
      text: factValue.known
        ? (typeof factValue.value === "number" ? compact(factValue.value) : String(factValue.value ?? "null"))
        : "UNKNOWN",
      cls: factValue.known ? "known" : "unknown",
      title: factValue.known ? String(factValue.value ?? "") : String(factValue.reason || "unknown"),
    };
  }
  return { text: String(factValue ?? "UNKNOWN"), cls: "" };
}

/** 仪表盘数值紧凑化：消除浮点尾噪（保留 8 位有效数字）。 */
function compact(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return String(Number(n.toPrecision(8)));
}

// ------------------------------------------------------------------ 稳定结构

function kpi(label, bind, value, titleText) {
  const v = value || {};
  const cls = ["kpi-value", v.cls].filter(Boolean).join(" ");
  return `<div class="kpi"${titleText ? ` title="${escapeHtml(titleText)}"` : ""}>` +
    `<div class="kpi-label">${escapeHtml(label)}</div>` +
    `<div class="${cls}" data-bind="${bind}"${v.title ? ` title="${escapeHtml(v.title)}"` : ""}>` +
    `${escapeHtml(v.text ?? "…")}</div></div>`;
}

function healthChip(label, bind, value) {
  const v = value || {};
  return `<div class="health-chip"><span class="kpi-label">${escapeHtml(label)}</span>` +
    `<span class="${escapeHtml(v.cls || "chip-value")}" data-bind="${bind}"` +
    `${v.title ? ` title="${escapeHtml(v.title)}"` : ""}>${escapeHtml(v.text ?? "…")}</span></div>`;
}

function statusLine(values, snapshot) {
  const id = snapshot.runtime;
  const modeTagClass = `tag mode-${String(id.mode || "").toLowerCase()}`;
  const pill = values.overall_health;
  return `
    <div class="statusline">
      <span class="${modeTagClass}" data-bind="mode">${escapeHtml(values.mode.text)}</span>
      <span data-bind="instrument">${escapeHtml(values.instrument.text)}</span>
      <span class="muted" data-bind="runtime_state">${escapeHtml(values.runtime_state.text)}</span>
      <span class="spacer"></span>
      <span class="${escapeHtml(pill.cls)}" data-bind="overall_health">${escapeHtml(pill.text)}</span>
    </div>`;
}

function ordersHtml(snapshot) {
  const orders = snapshot.execution?.active_orders || [];
  if (!orders.length) {
    return '<div class="row"><span class="k">active</span><span class="v known">0</span></div>';
  }
  return orders.slice(0, 8).map((o) => {
    const raw = o.client_order_id;
    const cid = raw && typeof raw === "object" ? (raw.known ? shortId(raw.value) : "UNKNOWN") : shortId(raw);
    const title = raw && typeof raw === "object" ? String(raw.value ?? "") : String(raw ?? "");
    return `<div class="row"><span class="k" title="${escapeHtml(title)}">${escapeHtml(cid)}</span>` +
      `<span class="v">${escapeHtml(o.side || "")} ${escapeHtml(o.status || "")} @ ${fact(o.price)}</span></div>`;
  }).join("");
}

function activityHtml(decisions) {
  if (!decisions.length) {
    return '<div class="row"><span class="k">activity</span>' +
      '<span class="v unknown">UNKNOWN (nothing recorded)</span></div>';
  }
  return decisions.slice(-5).reverse().map((d) =>
    `<div class="row"><span class="k">${escapeHtml(String(d.ts ?? ""))}</span>` +
    `<span class="v">${escapeHtml(d.action || "")} ${escapeHtml(d.side || "")}</span></div>`).join("");
}

function diagnosticsHtml(snapshot, execStatus) {
  const mc = snapshot.market_connector_health || {};
  const pc = snapshot.private_connector_health || {};
  const ref = snapshot.reference_price || {};
  const row = (k, v) => `<div class="row"><span class="k">${escapeHtml(k)}</span><span class="v">${v}</span></div>`;
  return {
    connections:
      row("market connection", `${fact(mc.connection_state)} <span class="muted">${fact(mc.event_age_ms)} ms</span>`) +
      row("private connection", `${fact(pc.connection_state)} <span class="muted">${fact(pc.event_age_ms)} ms</span>`) +
      row("reference", `${fact(ref.price_type)} ${fact(ref.price)} · ${fact(ref.source)}`) +
      row("reference freshness (ms)", fact(ref.freshness_ms)),
    execution:
      row("execution health", `<span class="${healthClass(execStatus)}">${escapeHtml(execStatus)}</span>`) +
      row("uncertain exposure", fact(snapshot.execution?.uncertain_exposure)) +
      row("kill switch", fact(snapshot.risk?.kill_switch_mode)),
  };
}

function attentionActionsHtml(manifest) {
  const links =
    '<a class="action-link" href="#/activity/evidence?focus=blockers">Inspect issues</a>' +
    '<a class="action-link" href="#/activity">View activity</a>';
  const action = !manifest.unavailable && Array.isArray(manifest.actions)
    ? manifest.actions.find((a) => a.action_id === "runtime.request_reconciliation") : null;
  const state = action && (action.availability?.state || action.availability?.value || action.availability);
  return links + (state === "AVAILABLE"
    ? '<button id="monitor-reconcile" data-confirm="1">Reconcile</button>' : "");
}

function primaryHtml(model, values) {
  const attention = values.attention;
  return `
  <section class="wide monitor-primary" id="monitor-primary">
    ${statusLine(values, model.snapshot)}
    <div class="kpi-row">
      ${kpi("equity", "equity", values.equity)}
      ${kpi("realized PnL", "realized_pnl", values.realized_pnl)}
      ${kpi("unrealized PnL", "unrealized_pnl", values.unrealized_pnl)}
      ${kpi("position", "position", values.position)}
      ${kpi("open order exposure", "exposure", values.exposure, "pending (active) open-order exposure")}
    </div>
    <div class="health-row">
      ${healthChip("Market", "market_health", values.market_health)}
      ${healthChip("Strategy", "strategy_state", values.strategy_state)}
      ${healthChip("Execution", "execution_health", values.execution_health)}
      ${healthChip("Readiness", "readiness", values.readiness)}
    </div>
    <div class="attention" id="monitor-attention">
      <div class="attention-summary" data-bind="attention" data-state="${escapeHtml(attention.state)}">` +
        `<span>${escapeHtml(attention.text)} </span>` +
        '<a class="surface-link" href="#/activity/evidence?focus=blockers">evidence →</a></div>' +
      `<div class="attention-actions" id="monitor-actions">${attentionActionsHtml(model.manifest)}</div>
    </div>
  </section>`;
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 26 ? "…" + text.slice(-24) : text;
}

export async function render() {
  const model = await gatherModel();
  lastTimeline = await fetchOrUnavailable(ENDPOINTS.portfolioTimeline).catch(() => null);
  const values = bindValues(model);
  const diag = diagnosticsHtml(model.snapshot, model.execStatus);
  const html = primaryHtml(model, values) +
    `<section class="wide monitor-secondary" id="monitor-trends">
      <h2>Trends</h2>${sparklinesSection()}</section>` +
    `<section class="monitor-secondary" id="monitor-orders">
      <h2>Active orders <a class="surface-link" href="#/activity/orders">all →</a></h2>
      <div data-list="orders">${ordersHtml(model.snapshot)}</div>
    </section>
    <section class="monitor-secondary" id="monitor-activity">
      <h2>Recent activity <a class="surface-link" href="#/activity">all →</a></h2>
      <div data-list="activity">${activityHtml(model.decisions)}</div>
    </section>` +
    `<section class="wide monitor-diagnostic">
      <details class="diagnostics"><summary>Connections / reference / execution internals</summary>
        <div id="diag-connections">${diag.connections}</div>
        <div id="diag-execution">${diag.execution}</div>
        <div class="diag-links">
          <a href="#/system">System（完整工程诊断）</a> ·
          <a href="#/activity/evidence">Evidence（因果与原始事实）</a>
        </div>
      </details>
    </section>`;
  // 首帧图表/按钮挂载推迟到节点就位（由 console.mount 钩子调用）
  queueMicrotask(() => { mountSparklinesOnce(); bindReconcile(); });
  return html;
}

// ------------------------------------------------------------------ targeted patch

const setText = (el, value) => { if (el && el.textContent !== value) el.textContent = value; };

function patchBound(el, value) {
  if (!el || !value) return;
  setText(el, value.text ?? "");
  if (value.cls !== undefined) el.className = value.cls;
  if (value.title !== undefined) {
    if (value.title) el.title = value.title; else el.removeAttribute("title");
  }
  if (value.state !== undefined) el.dataset.state = value.state;
}

export async function refresh() {
  let model;
  try {
    model = await gatherModel();
  } catch {
    const pill = document.querySelector('[data-bind="overall_health"]');
    if (pill) { setText(pill, "SNAPSHOT UNAVAILABLE"); pill.className = "health-pill bad"; }
    return;
  }
  lastTimeline = await fetchOrUnavailable(ENDPOINTS.portfolioTimeline).catch(() => null);
  const values = bindValues(model);
  for (const [bind, value] of Object.entries(values)) {
    patchBound(document.querySelector(`[data-bind="${bind}"]`), value);
  }
  // 列表节点（只替换列表内容，卡片容器身份不变）
  replaceIfChanged('[data-list="orders"]', ordersHtml(model.snapshot));
  replaceIfChanged('[data-list="activity"]', activityHtml(model.decisions));
  const diag = diagnosticsHtml(model.snapshot, model.execStatus);
  replaceIfChanged("#diag-connections", diag.connections);
  replaceIfChanged("#diag-execution", diag.execution);
  replaceIfChanged("#monitor-actions", attentionActionsHtml(model.manifest));
  bindReconcile();
  mountSparklinesOnce();
}

function replaceIfChanged(selector, html) {
  const host = document.querySelector(selector);
  if (host && host.innerHTML !== html) host.innerHTML = html;
}

function mountSparklinesOnce() {
  // 动态 import 避免 Node 渲染 harness 加载 echarts；浏览器中 setOption 复用实例
  import("/ui/monitor/sparklines.js").then(({ mountSparklines }) => mountSparklines(lastTimeline))
    .catch(() => {});
}

function bindReconcile() {
  const button = document.getElementById("monitor-reconcile");
  if (!button || button.dataset.bound) return;
  button.dataset.bound = "1";
  button.addEventListener("click", async () => {
    button.disabled = true;
    let outcome = await invoke("runtime.request_reconciliation", { surface: "monitor" });
    if (outcome.status === "CONFIRMATION_REQUIRED") {
      outcome = await invoke("runtime.request_reconciliation",
        { surface: "monitor", confirmation: outcome.confirmation_id });
    }
    button.textContent = String(outcome.status);
    button.disabled = false;
  });
}

export async function mount() {
  mountSparklinesOnce();
  bindReconcile();
}
