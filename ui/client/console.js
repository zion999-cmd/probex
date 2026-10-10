/** Console 外壳：5 个 Surface 导航 + 全局 Header + 全局 Blocker Strip（P0001.12.1）。 */
import { ENDPOINTS, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact } from "/ui/client/render.js";
import { SURFACES, surfaceForHash } from "/ui/app/surfaces.js";
import { BLOCKER_SECTION, resolveRoute, surfaceHash } from "/ui/app/navigation.js";
import { mountAssistant } from "/ui/assistant/drawer.js";

export const POLL_INTERVAL_MS = 2000;
// P0001.10.2 §6：正文“可以轮询 snapshot”。轮询（非 WS 推送，WS 按提案延期）。
export const SURFACE_REFRESH_MS = 5000;
let surfaceRefreshTimer = null;
let currentRouteKey = "";

function currentRoute() {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/");
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
  const strip = document.getElementById("blockers");
  try {
    const snapshot = await fetchSnapshot();
    const id = snapshot.runtime;
    const blockers = snapshot.blockers || [];
    const blocking = blockers.filter((b) => b.severity === "BLOCKING");
    banner.innerHTML = [
      `<span class="tag ${id.mode === "LIVE" ? "bad" : ""}">${id.mode}</span>`,
      id.environment, id.venue, id.symbol, `runtime=${escapeHtml(id.runtime_id)}`,
      `health=${snapshot.market.healthy.known ? (snapshot.market.healthy.value ? "HEALTHY" : "UNHEALTHY") : "UNKNOWN"}`,
      `data_ts=${fact(id.data_timestamp)}`,
    ].join(" · ");
    strip.innerHTML = blockers.length === 0
      ? '<span class="known">no blockers</span>'
      : blockers.map((b) => {
          const cls = b.severity === "BLOCKING" ? "bad" : "unknown";
          // F-16：Blocker → System 对应 section（canonical owner 映射）
          const href = surfaceHash("system", BLOCKER_SECTION[b.owner] || "execution");
          const explanation = b.explanation || {};
          const title = explanation.explanation || b.message;
          return `<a class="${cls}" href="${href}" title="${escapeHtml(title)}">${escapeHtml(b.owner)}:${escapeHtml(b.reason_code)}</a>`;
        }).join(" · ");
    document.getElementById("strip").dataset.blocking = String(blocking.length);
  } catch (error) {
    banner.innerHTML = `<span class="bad">snapshot unavailable: ${escapeHtml(String(error))}</span>`;
    strip.innerHTML = '<span class="bad">blockers UNKNOWN</span>';
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
