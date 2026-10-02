/** Market Surface：Live / Replay / Run Review 三个二级入口。
 *
 * 主图区 = 真实 K 线工作台（klinecharts + extension）：candles / 指标 / 画线 / Probex semantic markers；
 * 下方保留 L2 heatmap（微观结构）与 order book / recent trades / prediction / features。
 */
import { ENDPOINTS, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, rows, section, table, valueView } from "/ui/client/render.js";
import { HEATMAP_NOTE, drawHeatmap } from "/ui/pages/market/heatmap.js";
import { boundsNote, decisionTable, executionTable, tradesTable } from "/ui/pages/market/overlays.js";
import { replayControls } from "/ui/pages/market/replay.js";
import { featurePanels, healthStrip } from "/ui/pages/market/timeline.js";
import { mountWorkbench, TIMEFRAMES, toolbarHtml } from "/ui/pages/market/workbench.js";
import { mountPredictionPanel, predictionSection } from "/ui/pages/market/prediction_panel.js";
import { updateSelection } from "/ui/client/selection.js";

export const title = "Market";
export const slug = "market";

export const VIEWS = ["live", "replay", "run-review"];

function nav(active) {
  return VIEWS.map((view) =>
    `<a href="#/market/${view}" class="${view === active ? "active" : ""}">${escapeHtml(view)}</a>`).join(" · ");
}

function orderBookRows(depth) {
  const payload = depth && depth.depth ? depth.depth : null;
  if (!payload) return rows([["order book", '<span class="unknown">UNKNOWN (no depth projection)</span>']]);
  const last = (payload.cells || []).slice(-12).reverse();
  return table(["side", "price", "visible qty"], last.map((cell) =>
    [escapeHtml(cell.side), escapeHtml(String(cell.price)), escapeHtml(String(cell.quantity))]));
}

export async function render(rest = []) {
  const active = VIEWS.includes(rest[0]) ? rest[0] : "live";
  const runId = rest[1] || null;
  const focusTs = active !== "run-review" && rest[1] && /^\d+$/.test(String(rest[1])) ? String(rest[1]) : null;
  const header = section("Market view", nav(active) + (runId
    ? `<div class="row"><span class="k">run</span><span class="v">${escapeHtml(runId)}</span></div>` : "")
    + (focusTs ? `<div class="row"><span class="k">focused timestamp</span><span class="v">${escapeHtml(focusTs)}</span></div>` : ""));

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
      section("Run review notes", rows([
        ["candles", "K 线聚合只对**当前已接线的 market 缓冲**可用；历史 run 的逐笔/盘口未持久化 ⇒ 不伪造 K 线"],
        ["run_id", escapeHtml(payload.run_id || runId)],
      ])) +
      section("Projection bounds", boundsNote(payload));
  }

  const [snapshot, candles1m, trades, depth, health, overlays, evidence, timeline] = await Promise.all([
    fetchSnapshot(),
    fetchJson(`${ENDPOINTS.marketCandles}?interval=1m&limit=300`),
    fetchJson(ENDPOINTS.marketTrades), fetchJson(ENDPOINTS.marketDepth),
    fetchJson(ENDPOINTS.marketHealth), fetchJson(ENDPOINTS.marketOverlays),
    fetchJson(ENDPOINTS.evidence), fetchJson(ENDPOINTS.marketTimeline),
  ]);
  const trace = (evidence.evidence || {}).trace || [];

  const chart = `
    <section class="wide">
      <h2>K-line workbench · ${escapeHtml(snapshot.runtime.symbol)} · ${snapshot.runtime.mode}</h2>
      <div id="chart-toolbar" class="toolbar"></div>
      <div id="kline-chart" class="chart-host" style="height:460px"></div>
      <div class="row"><span class="k">candles source</span><span class="v">${escapeHtml((candles1m.candles || {}).source || "unknown")}</span>
        <span class="k">note</span><span class="v">${escapeHtml((candles1m.candles || {}).note || "")}</span></div>
      <div class="row"><span class="k">semantic markers</span><span class="v">点击 K 线上的 prediction / decision / risk / order / ack / fill / cancel / reconciliation 标记 → Activity / Evidence / Raw Facts</span></div>
    </section>`;

  const body = chart +
    `<section><h2>Order book / microstructure</h2>${orderBookRows(depth)}</section>` +
    `<section><h2>Recent trades</h2>${tradesTable(trades.trades)}</section>` +
    `<section class="wide"><h2>L2 depth heatmap</h2>` +
      '<canvas id="heatmap" width="1200" height="320"></canvas>' +
      `<div class="row"><span class="k">note</span><span class="v unknown">${escapeHtml(HEATMAP_NOTE)}</span></div></section>` +
    `<section class="wide"><h2>Prediction</h2><div id="market-prediction"></div></section>` +
    `<section class="wide"><h2>Features</h2>` + featurePanels(timeline.timeline) + '</section>' +
    `<section class="wide"><h2>Data quality</h2>` + healthStrip(health.health) + '</section>' +
    `<section><h2>Decisions</h2>${decisionTable(overlays.overlays)}</section>` +
    `<section><h2>Execution</h2>${executionTable(overlays.overlays)}</section>` +
    `<section><h2>Replay controls</h2><div id="replay-controls"></div>` +
      '<div class="muted">play / pause / step / speed / seek 与 K 线 cursor/time window 同步</div></section>' +
    section("Projection bounds", boundsNote(depth));

  lastMarket = { candles1m, trace, focusTs, depth, symbol: snapshot.runtime.symbol };
  return header + body;
}

let lastMarket = null;

export async function mount() {
  const context = lastMarket;
  if (!context) return;
  const { candles1m, trace, focusTs, depth, symbol } = context;
  const snapshot = await fetchSnapshot();

    const canvas = document.getElementById("heatmap");
    if (canvas) drawHeatmap(canvas, depth.depth);

    let timeframe = "1m";
    let workbench = null;
    const candlesFor = async (interval) => {
      const payload = await fetchJson(`${ENDPOINTS.marketCandles}?interval=${interval}&limit=300`);
      return payload.candles || { candles: [] };
    };
    const refreshChart = async (interval) => {
      timeframe = interval;
      const [series, tracePayload] = await Promise.all([candlesFor(interval), fetchJson(ENDPOINTS.evidence)]);
      const nextTrace = (tracePayload.evidence || {}).trace || [];
      if (workbench) workbench.refresh({ candles: series.candles || [], trace: nextTrace,
                                         source: series.source });
    };
    workbench = mountWorkbench(document.getElementById("kline-chart"),
                               document.getElementById("chart-toolbar"), {
      candles: candles1m.candles ? candles1m.candles.candles : [],
      trace,
      timeframe,
      focusTs,
      symbol: snapshot.runtime.symbol,
      source: (candles1m.candles || {}).source,
      onTimeframe: (interval) => { refreshChart(interval); },
      onSelection: (selection) => {
        updateSelection({ timestamp: selection.timestamp, timeframe: selection.timeframe,
                          surface: "market", symbol: snapshot.runtime.symbol });
      },
    });
    if (!workbench) {
      const host = document.getElementById("kline-chart");
      if (host) host.innerHTML = '<div class="unknown">UNKNOWN (chart library unavailable in this environment)</div>';
    }

    const controls = document.getElementById("replay-controls");
    if (controls) replayControls(controls, {
      onCommand: async (verb) => { await refreshChart(timeframe); },
    });

    const predictionHost = document.getElementById("market-prediction");
    if (predictionHost) {
      predictionHost.innerHTML = predictionSection(snapshot.prediction);
      const host = document.getElementById("prediction-horizons");
      const mounted = mountPredictionPanel(host, snapshot.prediction);
      if (!mounted && host && snapshot.prediction.horizons && snapshot.prediction.horizons.known) {
        host.innerHTML = '<div class="unknown">UNKNOWN (chart library unavailable)</div>';
      }
    }
  
}
