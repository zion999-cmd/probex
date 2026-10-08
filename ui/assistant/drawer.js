/** Assistant Drawer（P0001.12.3 §7）：五个 Surface 的全局能力，不是第六个一级页面。 */
import { ENDPOINTS, explain, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml } from "/ui/client/render.js";
import { currentSelection } from "/ui/assistant/context.js";
import { invoke, loadSuggestions, renderActions } from "/ui/assistant/actions.js";

export const DRAWER_ID = "assistant-drawer";

export function mountAssistant(container) {
  const drawer = document.createElement("aside");
  drawer.id = DRAWER_ID;
  drawer.innerHTML = `
    <header><strong>Probex Assistant</strong>
      <button id="assistant-toggle" title="collapse">–</button></header>
    <div id="assistant-body">
      <div class="row"><span class="k">context</span><span class="v">…</span></div>
      <div id="assistant-actions"></div>
      <div id="assistant-status"></div>
    </div>`;
  container.appendChild(drawer);
  drawer.querySelector("#assistant-toggle").addEventListener("click", () => {
    drawer.classList.toggle("collapsed");
  });
  refreshAssistant();
  window.addEventListener("hashchange", refreshAssistant);
  // closure slice：图表选中变化（crosshair）时刷新 Assistant 上下文（去抖）
  let timer = null;
  window.addEventListener("probex:selection", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(refreshAssistant, 350);
  });
  return drawer;
}

/** P0001.15 §27：把 instrument / venue / reference price 事实与可回答的问题放进 drawer。 */
async function renderInstrumentContext(body, context) {
  let host = document.getElementById("assistant-instrument");
  if (!host) {
    host = document.createElement("div");
    host.id = "assistant-instrument";
    body.insertBefore(host, document.getElementById("assistant-actions"));
  }
  const instrumentId = context.instrument_id && context.instrument_id.known ? context.instrument_id.value : null;
  const recap = [context.instrument_id, context.asset_class, context.product_type, context.venue_id]
    .map((value) => (value && value.known ? escapeHtml(String(value.value)) : '<span class="unknown">UNKNOWN</span>'))
    .join(" · ");
  const reference = context.reference_price && context.reference_price.known
    ? escapeHtml(String(context.reference_price.value))
    : `<span class="unknown">UNKNOWN (${escapeHtml((context.reference_price_reason || {}).value || "no mark source")})</span>`;
  host.innerHTML = `
    <div class="row"><span class="k">instrument</span><span class="v">${recap}</span></div>
    <div class="row"><span class="k">MARK</span><span class="v">${reference} ← ${escapeHtml(
      (context.reference_price_source || {}).value || "UNKNOWN")}</span></div>
    <div class="row"><span class="k">venue links</span><span class="v">${escapeHtml(
      (context.market_connector || {}).value || "UNKNOWN")} / ${escapeHtml(
      (context.execution_connector || {}).value || "UNKNOWN")}</span></div>
    <div id="assistant-questions"></div>`;
  const questions = [
    ["instrument", instrumentId, "Spot 还是 Perpetual？"],
    ["reference_price", instrumentId, "当前 MARK 从哪里来？"],
    ["reference_price", instrumentId, "为什么 MARK UNKNOWN 会 fail-closed？"],
    ["order", null, "订单来自哪个 decision？"],
    ["venue", instrumentId, "通过哪个 execution connector 执行？"],
  ];
  const questionsHost = document.getElementById("assistant-questions");
  questionsHost.innerHTML = questions
    .map(([kind, identity, label], index) =>
      `<button data-question="${index}">${escapeHtml(label)}</button>`)
    .join(" ");
  questionsHost.querySelectorAll("button[data-question]").forEach((button) => {
    button.addEventListener("click", () => showAnswer(body, questions[Number(button.dataset.question)]));
  });
}

async function showAnswer(body, [kind, identity, label]) {
  let host = document.getElementById("assistant-answer");
  if (!host) {
    host = document.createElement("div");
    host.id = "assistant-answer";
    body.appendChild(host);
  }
  host.innerHTML = `<div class="row"><span class="k">${escapeHtml(label)}</span>` +
    '<span class="v">…</span></div>';
  try {
    const snapshot = await fetchSnapshot();
    const order = ((snapshot.execution || {}).active_orders || [])[0];
    const resolvedIdentity = identity || (order && order.client_order_id) ||
      ((order && order.decision_id && order.decision_id.value) || "");
    if (!resolvedIdentity) {
      // 没有订单事实时不得伪造 identity，也不发无效请求
      host.querySelector(".v").innerHTML =
        '<span class="unknown">UNKNOWN（当前没有订单事实可关联：无挂单/未提交）</span>';
      return;
    }
    const explanation = await explain(kind, resolvedIdentity);
    const answers = explanation.answers || {};
    const text = Object.values(answers).join(" · ");
    host.querySelector(".v").innerHTML = text
      ? escapeHtml(text)
      : '<span class="unknown">UNKNOWN (no answer from recorded facts)</span>';
  } catch (error) {
    host.querySelector(".v").innerHTML = `<span class="unknown">UNKNOWN (${escapeHtml(String(error))})</span>`;
  }
}

export async function refreshAssistant() {
  const body = document.getElementById("assistant-body");
  if (!body) return;
  const selection = currentSelection();
  const contextRow = body.querySelector(".row .v");
  const actionsHost = document.getElementById("assistant-actions");
  try {
    const { context, suggested } = await loadSuggestions(selection);
    contextRow.innerHTML = `${escapeHtml(context.surface)} · ${escapeHtml(context.runtime.mode)} · ` +
      `${escapeHtml(context.runtime.runtime_id)} · blockers=${context.active_blockers.length}`;
    await renderInstrumentContext(body, context);
    actionsHost.innerHTML = renderActions(suggested);
    actionsHost.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const status = document.getElementById("assistant-status");
        let outcome = await invoke(button.dataset.action, { surface: selection.surface });
        if (outcome.status === "CONFIRMATION_REQUIRED") {
          status.innerHTML = `<span class="unknown">confirmation required</span>`;
          outcome = await invoke(button.dataset.action, { surface: selection.surface,
                                                          confirmation: outcome.confirmation_id });
        }
        const cls = outcome.status === "SUCCEEDED" ? "known" : "bad";
        status.innerHTML = `<span class="${cls}">${escapeHtml(outcome.status)}</span> ` +
          `${escapeHtml(outcome.reason_code || "")}`;
      });
    });
  } catch (error) {
    contextRow.innerHTML = `<span class="unknown">UNKNOWN (${escapeHtml(String(error))})</span>`;
    actionsHost.innerHTML = "";
  }
}
