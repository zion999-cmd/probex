/** Market workbench（P0001.12）：L2 depth heatmap + features + quality + trades/overlays + replay controls。 */
import { ENDPOINTS, fetchJson } from "/ui/client/api.js";
import { escapeHtml, section } from "/ui/client/render.js";
import { HEATMAP_NOTE, drawHeatmap } from "/ui/pages/market/heatmap.js";
import { boundsNote, decisionTable, executionTable, tradesTable } from "/ui/pages/market/overlays.js";
import { replayControls } from "/ui/pages/market/replay.js";
import { featurePanels, healthStrip } from "/ui/pages/market/timeline.js";

export const title = "Market";
export const slug = "market";

export async function render() {
  const [timeline, depth, trades, health, overlays] = await Promise.all([
    fetchJson(ENDPOINTS.marketTimeline), fetchJson(ENDPOINTS.marketDepth),
    fetchJson(ENDPOINTS.marketTrades), fetchJson(ENDPOINTS.marketHealth),
    fetchJson(ENDPOINTS.marketOverlays),
  ]);
  const html =
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
  return html;
}
