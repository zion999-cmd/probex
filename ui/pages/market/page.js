/** Market Surface：Live / Replay / Run Review 三个二级入口。
 *
 * 主图区 = 真实 K 线工作台（klinecharts + extension）：candles / 指标 / 画线 / Probex semantic markers；
 * 下方保留 L2 heatmap（微观结构）与 order book / recent trades / prediction / features。
 */
import { ENDPOINTS, fetchJson, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, rows, section, table, valueView } from "/ui/client/render.js";
import { HEATMAP_NOTE, drawHeatmap } from "/ui/pages/market/heatmap.js";
import { boundsNote, decisionTable, executionTable, tradesTable } from "/ui/pages/market/overlays.js";
import { replayControls } from "/ui/pages/market/replay.js";
import { featurePanels, healthStrip } from "/ui/pages/market/timeline.js";
import { BACKEND_FACT_INDICATORS } from "/ui/pages/market/indicators.js";
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

/** P0001.15 §23：Market 页面明确 instrument / venue / market data source / MARK 状态。 */
function instrumentVenueSection(snapshot) {
  const instrument = snapshot.instrument || {};
  const venue = snapshot.venue || {};
  const reference = snapshot.reference_price || {};
  const connector = snapshot.market_connector_health || {};
  const extras = connector.extras && connector.extras.known ? connector.extras.value : {};
  return section("Instrument / market data source", rows([
    ["instrument", `${fact(instrument.instrument_id)} · ${fact(instrument.asset_class)} · ${fact(instrument.product_type)}`],
    ["venue", `${fact(venue.venue_id)} (${fact(venue.environment)})`],
    ["market connector", `${fact(connector.connector_id)} · ${fact(connector.connection_state)}`],
    ["data source", escapeHtml(String(extras.data_source || "UNKNOWN"))],
    ["mark / reference price", `${fact(reference.price_type)} ${fact(reference.price)}`],
    ["reference source", fact(reference.source)],
    ["reference freshness (ms)", fact(reference.freshness_ms)],
    ["reference reason", fact(reference.reason)],
  ]));
}

export async function render(rest = []) {
  const active = VIEWS.includes(rest[0]) ? rest[0] : "live";
  const runId = rest[1] || null;
  const focusTs = active !== "run-review" && rest[1] && /^\d+$/.test(String(rest[1])) ? String(rest[1]) : null;
  const header = section("Market view", nav(active) + (runId
    ? `<div class="row"><span class="k">run</span><span class="v">${escapeHtml(runId)}</span></div>` : "")
    + (focusTs ? `<div class="row"><span class="k">focused timestamp</span><span class="v">${escapeHtml(focusTs)}</span></div>` : ""));

  if (active === "run-review") {
    // run-review 需要当前 runtime identity（用于判断该 run 是否为当前运行）
    const current = await fetchSnapshot();
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
    if (payload.source === "durable") {
      // P0001.17 §9：历史 run ⇒ 用该 run 的**持久化事实**画图并定位（只读；缺失事实如实标注）
      const candles = ((payload.candles || {}).candles) || [];
      lastRunReview = { runId: payload.run_id || runId, candles, facts: payload.facts || [] };
      const factRows = (payload.facts || []).slice(-60).reverse().map((item) => [
        String(item.ts), escapeHtml(String(item.kind || "")),
        escapeHtml(String(item.decision_id || item.client_order_id || "")),
        escapeHtml(String(item.status || item.mode || "")),
        `<button data-locate="${escapeHtml(String(item.ts))}">locate on chart</button>`]);
      return header +
        `<section class="wide"><h2>Recorded market chart (durable facts)</h2>` +
        `<div id="wb-toolbar-runreview" class="wb-toolbar"></div>` +
        `<div id="kline-chart-runreview" class="kline-chart"></div>` +
        `<div class="muted">source=<code>durable</code> · ${escapeHtml(String(payload.note || ""))}</div></section>` +
        section("Run facts (locate on chart)", factRows.length
          ? table(["ts", "kind", "identity", "state", "locate"], factRows)
          : rows([["facts", '<span class="unknown">UNKNOWN（该 run 未记录 decision/order 事实）</span>']])) +
        section("Recorded market timeline", featurePanels(payload.timeline)) +
        section("Run review notes", rows([
          ["source", "durable（该 run 的持久化事实，不是当前接线缓冲）"],
          ["volume", '<span class="unknown">UNAVAILABLE</span> <span class="muted">历史 run 只记录盘口 mid，未持久化逐笔成交 ⇒ 不伪造成交量</span>'],
          ["cross-run", "不同 run 不共享缓冲/选择状态（切换 run 会清空 selected decision/order/fill）"],
        ])) +
        section("Projection bounds", boundsNote(payload));
    }
    const isCurrentRun = payload.run_id === current.runtime.runtime_id;
    const timelinePoints = (payload.timeline || {}).points || [];
    const firstTs = timelinePoints.length ? timelinePoints[0].ts : null;
    const lastTs = timelinePoints.length ? timelinePoints[timelinePoints.length - 1].ts : null;
    const locate = isCurrentRun && lastTs
      ? rows([
        ["chart focus", `<a href="#/market/live/${encodeURIComponent(String(lastTs))}">focused @ ${lastTs}</a>` +
          ` · <a href="#/market/live/${encodeURIComponent(String(firstTs))}">window start @ ${firstTs}</a> ` +
          `<span class="muted">（在同一 run 的 K 线工作台上定位；Decision/Order/Fill 也可从 Activity 跳转）</span>`],
        ["run identity", `<code>${escapeHtml(payload.run_id || runId)}</code> = current runtime run`],
      ])
      : rows([
        ["chart focus", '<span class="unknown">UNAVAILABLE（该 run 的逐笔/盘口事实未持久化 ⇒ 不跳转到无数据的时间）</span>'],
        ["run identity", `<code>${escapeHtml(payload.run_id || runId)}</code>` +
          `<span class="muted"> ≠ current runtime run ${escapeHtml(current.runtime.runtime_id)}</span>`],
        ["恢复入口", "Run Review 只能看**已记录的时间轴/摘要**；要定位图表请选择当前运行（Market → live）"],
      ]);
    return header + section("Recorded market timeline", featurePanels(payload.timeline)) +
      section("Locate on chart", locate) +
      section("Run review notes", rows([
        ["recorded facts", "run summary + 有界 market timeline（仅当前已接线 run 有）"],
        ["not persisted", "历史 run 的逐笔成交/盘口快照未持久化 ⇒ 不提供 K 线与时间定位，也不伪造"],
        ["cross-run", "不同 run 之间不共享任何缓冲/选择状态（切换 run 会清空 selected decision/order/fill）"],
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
    section("Projection bounds", boundsNote(depth)) + instrumentVenueSection(snapshot) +
    section("Indicators not available (backend has no such fact)", rows(
      BACKEND_FACT_INDICATORS.map((item) => [item.name,
        `<span class="muted">${escapeHtml(item.source)}</span>`])));

  lastMarket = { candles1m, trace, focusTs, depth, symbol: snapshot.runtime.symbol };
  return header + body;
}

let lastMarket = null;
let lastRunReview = null;

export async function mount(rest = []) {
  // P0001.17 §9：Run Review 的记录事实图表（历史 run；只读展示 + 事实定位）
  if (rest[0] === "run-review" && lastRunReview && lastRunReview.candles.length) {
    const workbench = mountWorkbench(document.getElementById("kline-chart-runreview"),
                                    document.getElementById("wb-toolbar-runreview"), {
      candles: lastRunReview.candles, trace: [], timeframe: "1m", symbol: "BTCUSDT",
      source: "durable run facts (mid-only)", indicators: ["MA", "EMA", "VOL"],
    });
    document.querySelectorAll("button[data-locate]").forEach((button) => {
      button.addEventListener("click", () => workbench && workbench.setFocus(Number(button.dataset.locate)));
    });
    updateSelection({ surface: "market", run: lastRunReview.runId, timestamp: null,
                      decision: null, order: null, fill: null });
  }
  const context = lastMarket;
  if (!context) return;
  const { candles1m, trace, focusTs, depth, symbol } = context;
  const snapshot = await fetchSnapshot();

    const canvas = document.getElementById("heatmap");
    if (canvas) drawHeatmap(canvas, depth.depth);

    let timeframe = "1m";
    // P0001.17 §3：把"当前 run + 图表选择"写入 selection（切换 run 时清空已选对象，避免串 run）
    updateSelection({ surface: "market", run: snapshot.runtime.runtime_id, symbol: snapshot.runtime.symbol,
                      timestamp: focusTs || null, timeframe: "1m",
                      decision: null, order: null, fill: null });
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
      atrPoints: (((candles1m.candles || {}).indicators || {}).atr || {}).points || [],
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
