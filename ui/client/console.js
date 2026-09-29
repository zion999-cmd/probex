/** Console 外壳：路由 + 轮询（P0001.10.2 §6：先轮询 /api/v1/snapshot，不做 WebSocket）。 */
import { fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact } from "/ui/client/render.js";

const PAGES = ["overview", "market", "prediction", "strategy", "risk", "orders", "portfolio",
               "readiness", "evidence", "runs"];
export const POLL_INTERVAL_MS = 2000;

function currentSlug() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  return PAGES.includes(hash) ? hash : "overview";
}

function renderNav(active) {
  document.getElementById("nav").innerHTML = PAGES.map((slug) =>
    `<a href="#/${slug}" class="${slug === active ? "active" : ""}">${slug}</a>`).join("");
}

async function renderBanner() {
  try {
    const snapshot = await fetchSnapshot();
    const id = snapshot.runtime;
    document.getElementById("banner").innerHTML = [
      id.mode, id.environment, id.venue, id.symbol, `runtime=${escapeHtml(id.runtime_id)}`,
      `data_ts=${fact(id.data_timestamp)}`,
    ].join(" · ");
  } catch (error) {
    document.getElementById("banner").innerHTML = `<span class="bad">snapshot unavailable: ${escapeHtml(String(error))}</span>`;
  }
}

async function renderPage() {
  const slug = currentSlug();
  renderNav(slug);
  const view = document.getElementById("view");
  view.innerHTML = '<section><h2>loading</h2></section>';
  try {
    const module = await import(`/ui/pages/${slug}/page.js`);
    view.innerHTML = await module.render();
  } catch (error) {
    view.innerHTML = `<section><h2>error</h2><div class="row"><span class="k">page</span><span class="v bad">${escapeHtml(String(error))}</span></div></section>`;
  }
}

export function boot() {
  window.addEventListener("hashchange", renderPage);
  renderPage();
  renderBanner();
  setInterval(() => { renderBanner(); renderPage(); }, POLL_INTERVAL_MS);
}
