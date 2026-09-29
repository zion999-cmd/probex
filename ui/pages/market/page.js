/** Market Surface（P0001.12.1 §2）：Live / Replay / Run Review 三个二级入口，同一视觉语言。 */
import { ENDPOINTS, fetchJson } from "/ui/client/api.js";
import { escapeHtml, section } from "/ui/client/render.js";
import { HEATMAP_NOTE, drawHeatmap } from "/ui/pages/market/heatmap.js";
import { boundsNote, decisionTable, executionTable, tradesTable } from "/ui/pages/market/overlays.js";
import { replayControls } from "/ui/pages/market/replay.js";
import { featurePanels, healthStrip } from "/ui/pages/market/timeline.js";

export const title = "Market";
export const slug = "market";

export const VIEWS = ["live", "replay", "run-review"];

function nav(active) {
  return VIEWS.map((view) =>
    `<a href="#/market/${view}" class="${view === active ? "active" : ""}">${escapeHtml(view)}</a>`).join(" · ");
}

export async function render(rest = []) {
  const active = VIEWS.includes(rest[0]) ? rest[0] : "live";
  const runId = rest[1] || null;
  const header = section("Market view", nav(active) + (runId
    ? `<div class="row"><span class="k">run</span><span class="v">${escapeHtml(runId)}</span></div>` : ""));

  if (active === "run-review") {
    if (!runId) {
      return header + section("Run review", '<div class="row"><span class="k">select a run</span>' +
        '<span class="v unknown">UNKNOWN (open Performance → Runs → market view)</span></div>');
    }
    let payload;
    try {
      payload = await fetchJson(`/api/v1/runs/${encodeURIComponent(runId)}/market`);
    } catch (error) {
      return header + section("Run review",
        `<div class="row"><span class="k">recorded market</span><span class="v unknown">UNKNOWN (${escapeHtml(String(error))})</span></div>`);
    }
    return header + section("Recorded market timeline", featurePanels(payload.timeline)) +
      section("Projection bounds", boundsNote(payload));
  }

  const [timeline, depth, trades, health, overlays] = await Promise.all([
    fetchJson(ENDPOINTS.marketTimeline), fetchJson(ENDPOINTS.marketDepth),
    fetchJson(ENDPOINTS.marketTrades), fetchJson(ENDPOINTS.marketHealth),
    fetchJson(ENDPOINTS.marketOverlays),
  ]);
  const body =
    section("L2 depth heatmap", '<canvas id="heatmap" width="900" height="320"></canvas>' +
      `<div class="row"><span class="k">note</span><span class="v unknown">${escapeHtml(HEATMAP_NOTE)}</span></div>`) +
    section("Features (from projection)", featurePanels(timeline.timeline)) +
    section("Data quality timeline", healthStrip(health.health)) +
    section("Trades", tradesTable(trades.trades)) +
    section("Decisions", decisionTable(overlays.overlays)) +
    section("Execution", executionTable(overlays.overlays)) +
    section("Replay controls", '<div id="replay-controls"></div>') +
    section("Projection bounds", boundsNote(depth));
  queueMicrotask(() => {
    const canvas = document.getElementById("heatmap");
    if (canvas) drawHeatmap(canvas, depth.depth);
    const controls = document.getElementById("replay-controls");
    if (controls) replayControls(controls);
  });
  return header + body;
}
