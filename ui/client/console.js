/** Console 外壳：5 个 Surface 导航 + 全局 Header + 全局 Blocker Strip（P0001.12.1）。 */
import { ENDPOINTS, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact } from "/ui/client/render.js";
import { SURFACES, surfaceForHash } from "/ui/app/surfaces.js";
import { resolveRoute } from "/ui/app/navigation.js";
import { mountAssistant } from "/ui/assistant/drawer.js";

/**
 * P0018：raw blocker 日志条 → 单行聚合摘要。
 * 返回 {state, text, groups}：state ∈ OK/DEGRADED/BLOCKING/UNKNOWN；
 * groups 为 owner → 数量（用于 title/可访问名，不在条带上铺原始事件）。
 */
export function summarizeBlockers(blockers) {
  const list = Array.isArray(blockers) ? blockers : [];
  const groups = new Map();
  let blocking = 0;
  for (const item of list) {
    const owner = String((item && item.owner) || "UNKNOWN");
    groups.set(owner, (groups.get(owner) || 0) + 1);
    if (item && item.severity === "BLOCKING") blocking += 1;
  }
  let state = "OK";
  if (blocking > 0) state = "BLOCKING";
  else if (list.length > 0) state = "DEGRADED";
  const breakdown = [...groups.entries()].map(([owner, count]) => `${owner} ${count}`).join(" · ");
  const text = state === "OK"
    ? "● ALL CLEAR · 0 blockers"
    : `● ${state} · ${list.length} blocker${list.length === 1 ? "" : "s"}${breakdown ? ` · ${breakdown}` : ""}`;
  return { state, text, groups: Object.fromEntries(groups) };
}

export const POLL_INTERVAL_MS = 2000;
// P0001.10.2 §6：正文“可以轮询 snapshot”。轮询（非 WS 推送，WS 按提案延期）。
export const SURFACE_REFRESH_MS = 5000;
let surfaceRefreshTimer = null;
let currentRouteKey = "";

function currentRoute() {
  // 先剥 hash query：`#/activity/orders?decision=<id>` 的路径段只有 activity/orders
  const parts = String(window.location.hash || "").replace(/^#\/?/, "").split("?")[0]
    .split("/").filter((part) => part !== "");
  const surface = surfaceForHash(window.location.hash);
  return { surface, rest: parts.slice(1) };
}

function renderNav(surface) {
  document.getElementById("nav").innerHTML = SURFACES.map((item) =>
    `<a href="#/${item.slug}" class="${item.slug === surface.slug ? "active" : ""}" title="${escapeHtml(item.question)}">${escapeHtml(item.title)}</a>`
  ).join("");
}

async function renderHeaderAndBlockers() {
  const banner = document.getElementById("banner");
  const strip = document.getElementById("global-status");
  try {
    const snapshot = await fetchSnapshot();
    const id = snapshot.runtime;
    const blockers = snapshot.blockers || [];
    const blocking = blockers.filter((b) => b.severity === "BLOCKING");
    const modeClass = `mode-${String(id.mode || "").toLowerCase()}`;
    const health = snapshot.market.healthy.known
      ? (snapshot.market.healthy.value ? "HEALTHY" : "UNHEALTHY") : "UNKNOWN";
    // P0001.12.1 不变式 1：Mode 必须永远明显（PAPER/TESTNET/LIVE 视觉区分）。
    banner.innerHTML = [
      `<span class="tag ${modeClass}">${escapeHtml(id.mode)}</span>`,
      escapeHtml(id.environment), escapeHtml(id.venue),
      `${escapeHtml(id.symbol)} <span class="muted" title="traded symbol 由启动参数 --symbol 决定，运行中不可切换">(fixed @startup)</span>`,
      `runtime=${escapeHtml(id.runtime_id)}`,
      `health=${health}`,
      `data_ts=${fact(id.data_timestamp)}`,
      // 不可切换项必须**显示原因**，不得伪装成可操作控件。
      `<details class="why"><summary title="为什么这些字段不能直接切换">why read-only?</summary>` +
      `<div class="why-body">` +
      `mode / environment / venue / symbol 由**启动参数**决定（\`--mode\`、\`--symbol\`），` +
      `运行中更改会改变会计与风控 Owner 的归属，故不提供运行中切换；` +
      `要换标的：重新启动进程（单标的运行），多标的集合见 ` +
      `<a href="#/market/universe">Market → universe</a>（只读：集合由启动配置 \`market.universe.symbols\` 决定）。` +
      `可执行的运行中动作以 Action Manifest 的 availability 为准（见 ` +
      `<a href="#/system/capabilities">System → capabilities</a>）。</div></details>`,
    ].join(" · ");
    // P0018：全局条带只显示聚合摘要；原始 blocker/event 行属于 Activity/Evidence。
    const summary = summarizeBlockers(blockers);
    strip.textContent = summary.text;
    strip.dataset.state = summary.state;
    strip.title = summary.state === "OK"
      ? "no blockers"
      : Object.entries(summary.groups)
          .map(([owner, count]) => `${owner}: ${count}`).join("\n") +
        "\n\nopen Activity → Evidence for reasons and next steps";
    document.getElementById("strip").dataset.blocking =
      String(blockers.filter((b) => b.severity === "BLOCKING").length);
  } catch (error) {
    banner.innerHTML = `<span class="bad">snapshot unavailable: ${escapeHtml(String(error))}</span>`;
    strip.textContent = "● UNKNOWN · blockers unavailable";
    strip.dataset.state = "UNKNOWN";
  }
}

async function renderSurface() {
  const { surface, rest } = currentRoute();
  currentRouteKey = surface + "/" + rest.join("/");
  renderNav(surface);
  const view = document.getElementById("view");
  view.innerHTML = '<section><h2>loading</h2></section>';
  try {
    // F-16：detail route（legacy page）与 Surface page 共用同一解析（无死页面）
    const route = resolveRoute(surface, rest);
    const module = await import(route.module);
    view.innerHTML = await module.render(route.args);
    // 真实挂载钩子：页面在 DOM 插入**之后**再挂 chart / canvas / ECharts（queueMicrotask 会早于 innerHTML）
    if (typeof module.mount === "function") await module.mount(route.args);
  } catch (error) {
    view.innerHTML = `<section><h2>error</h2>` +
      `<div class="row"><span class="k">page</span><span class="v bad">${escapeHtml(String(error))}</span></div>` +
      `<div class="row"><span class="k">recovery</span><span class="v">` +
      `<a href="#/monitor">Monitor</a> · <a href="#/system/health">System health</a> · ` +
      `<a href="#" id="retry-page">retry this page</a>` +
      `<span class="muted">（若为 run/decision 相关错误：确认对象属于当前运行；历史 run 的市场/因果链未持久化）</span></span></div>` +
      `</section>`;
    const retry = document.getElementById("retry-page");
    if (retry) retry.addEventListener("click", (event) => { event.preventDefault(); renderSurface(); });
  }
}

export function boot() {
  const shell = document.querySelector("main");
  if (shell && !document.getElementById("assistant-drawer")) mountAssistant(document.body);
  window.addEventListener("hashchange", renderSurface);
  renderSurface();
  renderHeaderAndBlockers();
  setInterval(() => { renderHeaderAndBlockers(); }, POLL_INTERVAL_MS);
  // 正文定时轮询（交互安全：页面在用户操作中可通过 window.__probexUiPause 暂停）
  surfaceRefreshTimer = setInterval(() => { void maybeRefreshSurface(); }, SURFACE_REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (surfaceRefreshTimer) clearInterval(surfaceRefreshTimer);
    if (!document.hidden) {
      surfaceRefreshTimer = setInterval(() => { void maybeRefreshSurface(); }, SURFACE_REFRESH_MS);
    }
  });
}

// 交互保护：用户正在操作（鼠标按下/输入/打开 drill-down）时跳过本轮正文重渲染。
function surfaceInteractionPaused() {
  if (document.hidden) return true;
  if (window.__probexUiPause) return true;
  if (document.querySelector("input:focus, textarea:focus, select:focus")) return true;
  // 用户展开了 drill-down / 详情面板（页面标记 data-panel-open）
  if (document.querySelector("[data-panel-open]")) return true;
  return false;
}

async function maybeRefreshSurface() {
  if (surfaceInteractionPaused()) return;
  // 仅在 hash 未变时静默刷新（hash 变化由 hashchange 处理）
  const { surface, rest } = currentRoute();
  if (surface + "/" + rest.join("/") !== currentRouteKey) return;
  // 页面可提供非破坏 refresh（保留图表/用户画线）；否则整体重渲染
  try {
    const route = resolveRoute(surface, rest);
    const module = await import(route.module);
    if (typeof module.refresh === "function") {
      await module.refresh(route.args);
      return;
    }
  } catch (error) {
    /* fall through to full render */
  }
  await renderSurface();
}
