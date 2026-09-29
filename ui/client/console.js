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
  } catch (error) {
    view.innerHTML = `<section><h2>error</h2><div class="row"><span class="k">page</span><span class="v bad">${escapeHtml(String(error))}</span></div></section>`;
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
