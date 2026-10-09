/** Console 外壳：5 个 Surface 导航 + 全局 Header + 全局 Blocker Strip（P0001.12.1）。 */
import { ENDPOINTS, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact } from "/ui/client/render.js";
import { SURFACES, surfaceForHash } from "/ui/app/surfaces.js";
import { BLOCKER_SECTION, resolveRoute, surfaceHash } from "/ui/app/navigation.js";
import { mountAssistant } from "/ui/assistant/drawer.js";

export const POLL_INTERVAL_MS = 2000;

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
}
