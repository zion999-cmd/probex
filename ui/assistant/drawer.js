/** Assistant Drawer（P0001.12.3 §7）：五个 Surface 的全局能力，不是第六个一级页面。 */
import { ENDPOINTS, explain, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml } from "/ui/client/render.js";
import { currentSelection } from "/ui/assistant/context.js";
import { invoke, loadSuggestions, renderActions } from "/ui/assistant/actions.js";
import { answerQuestion } from "/ui/assistant/chat.js";

export const DRAWER_ID = "assistant-drawer";
/** P0018：drawer 默认收起；展开状态跨 surface/Monitor 刷新保留（会话内容仍仅内存）。 */
const DRAWER_STATE_KEY = "probex.assistant.open";

function drawerShouldBeOpen() {
  try { return sessionStorage.getItem(DRAWER_STATE_KEY) === "open"; } catch { return false; }
}

/** 会话记录（仅内存、仅本会话；刷新即清空——不伪装成持久化历史）。 */
const transcript = [];
const ANSWER_TIMEOUT_MS = 20000;

export function mountAssistant(container) {
  const drawer = document.createElement("aside");
  drawer.id = DRAWER_ID;
  // P0018：默认 collapsed；只有显式持久化的 open 才展开。
  drawer.classList.toggle("collapsed", !drawerShouldBeOpen());
  drawer.innerHTML = `
    <header><strong>Probex Assistant</strong>
      <button id="assistant-toggle" title="collapse">–</button></header>
    <div id="assistant-body">
      <div class="row"><span class="k">context</span><span class="v">…</span></div>
      <div id="assistant-actions"></div>
      <div id="assistant-thread"></div>
      <form id="assistant-form" autocomplete="off">
        <input id="assistant-input" type="text" name="q"
               placeholder="问：为什么现在没有下单？/ 订单来自哪个 decision？"
               aria-label="向助手提问" />
        <button id="assistant-send" type="submit">发送</button>
      </form>
      <div id="assistant-status"></div>
    </div>`;
  container.appendChild(drawer);
  drawer.querySelector("#assistant-toggle").addEventListener("click", () => {
    drawer.classList.toggle("collapsed");
    try {
      sessionStorage.setItem(DRAWER_STATE_KEY,
        drawer.classList.contains("collapsed") ? "closed" : "open");
    } catch { /* sessionStorage 不可用时退化为内存态，不影响主流程 */ }
  });
  drawer.querySelector("#assistant-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void submitQuestion();
  });
  refreshAssistant();
  renderThread();
  window.addEventListener("hashchange", refreshAssistant);
  // closure slice：图表选中变化（crosshair）时刷新 Assistant 上下文（去抖）
  let timer = null;
  window.addEventListener("probex:selection", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(refreshAssistant, 350);
  });
  return drawer;
}

/** 用户提问 → 真实只读能力 → 回显答案与来源；失败/无响应显式。 */
async function submitQuestion() {
  const input = document.getElementById("assistant-input");
  const status = document.getElementById("assistant-status");
  const send = document.getElementById("assistant-send");
  if (!input || !status) return;
  const question = input.value.trim();
  if (!question) { status.innerHTML = '<span class="unknown">请输入问题</span>'; return; }
  input.value = "";
  if (send) send.disabled = true;
  const entry = { question, state: "PENDING", text: "", sources: [] };
  transcript.push(entry);
  renderThread();
  status.innerHTML = '<span class="unknown">thinking…</span>';
  try {
    const context = currentSelection() || {};
    const outcome = await Promise.race([
      answerQuestion(question, context),
      new Promise((resolve) => setTimeout(() => resolve({ __timeout: true }), ANSWER_TIMEOUT_MS)),
    ]);
    if (outcome && outcome.__timeout) {
      entry.state = "NO_RESPONSE";
      entry.text = `未在 ${Math.round(ANSWER_TIMEOUT_MS / 1000)}s 内得到响应（后端可能未响应）。`;
      status.innerHTML = '<span class="bad">NO_RESPONSE</span>';
    } else if (outcome && outcome.error) {
      entry.state = "FAILED";
      entry.text = String(outcome.error);
      status.innerHTML = '<span class="bad">FAILED</span>';
    } else {
      entry.state = "ANSWERED";
      entry.text = outcome.text || "";
      entry.sources = outcome.sources || [];
      entry.surfaceHint = outcome.surfaceHint || null;
      status.innerHTML = '<span class="known">answered</span>' +
        (entry.sources.length ? ` <span class="muted">sources: ${entry.sources.map(escapeHtml).join(", ")}</span>` : "");
    }
  } catch (error) {
    entry.state = "FAILED";
    entry.text = String(error);
    status.innerHTML = `<span class="bad">FAILED</span> ${escapeHtml(String(error))}`;
  } finally {
    if (send) send.disabled = false;
    renderThread();
  }
}

/** 会话渲染：问题 + 答案 + 来源 + （若答案指向某 surface）跳转入口。 */
function renderThread() {
  const host = document.getElementById("assistant-thread");
  if (!host) return;
  if (!transcript.length) {
    host.innerHTML = '<div class="muted">可直接提问（只读解释 / 导航）。涉及下单、撤单、改风控等资本动作一律不可用。</div>';
    return;
  }
  host.innerHTML = transcript.slice(-6).map((entry) => {
    const stateClass = entry.state === "ANSWERED" ? "known" : (entry.state === "PENDING" ? "unknown" : "bad");
    const navigate = entry.surfaceHint
      ? ` <a href="#/${escapeHtml(entry.surfaceHint)}">前往 ${escapeHtml(entry.surfaceHint)}</a>`
      : "";
    const sources = entry.sources && entry.sources.length
      ? `<div class="muted">sources: ${entry.sources.map(escapeHtml).join(", ")}</div>` : "";
    return `<div class="assistant-turn">` +
      `<div class="row"><span class="k">you</span><span class="v">${escapeHtml(entry.question)}</span></div>` +
      `<div class="row"><span class="k">assistant</span><span class="v ${stateClass}">` +
      `${escapeHtml(entry.text).replace(/\n/g, "<br>")}</span></div>${sources}${navigate}</div>`;
  }).join("");
  host.scrollTop = host.scrollHeight;
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
    // currentSelection() 返回 {surface, selection:{...}} ⇒ 展平后传给 explain（否则真实选中不会到达后端）
    const context = currentSelection() || {};
    const selection = { ...(context.selection || {}), surface: context.surface || "monitor" };
    const explanation = await explain(kind, resolvedIdentity, selection);
    const answers = explanation.answers || {};
    const preferred = ["selected_object", "selected_order_state", "order_decision_link", "submit_outcome",
                       "user_stream_health", "reconciliation_state", "prediction_source"];
    const ordered = preferred.filter((key) => answers[key]).map((key) => answers[key])
      .concat(Object.entries(answers).filter(([key]) => !preferred.includes(key)).map(([, value]) => value));
    const text = ordered.join(" · ");
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
