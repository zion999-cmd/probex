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
import { renderSymbolDetail, renderUniverse } from "/ui/pages/market/universe.js";
import { updateSelection } from "/ui/client/selection.js";
import { hashQuery } from "/ui/app/navigation.js";
import {
  L2_HEATMAP_REASON, marketViewFacts, marketViewHash, mountSymbolSelector,
  normalizeSymbol, symbolSelectorHtml,
} from "/ui/pages/market/symbol_selector.js";

export const title = "Market";
export const slug = "market";

export const VIEWS = ["live", "replay", "run-review", "universe"];

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
  const focusTs = active !== "run-review" && rest[1] && /^\d+$/.test(String(rest[1])) ? String(rest[1]) : null;  const header = section("Market view", nav(active) + (runId
    ? `<div class="row"><span class="k">run</span><span class="v">${escapeHtml(runId)}</span></div>` : "")
    + (focusTs ? `<div class="row"><span class="k">focused timestamp</span><span class="v">${escapeHtml(focusTs)}</span></div>` : ""));

  if (active === "universe") {
    const body = rest[1]
      ? await renderSymbolDetail(String(rest[1]))
      : await renderUniverse();
    return header + body;
  }

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
      // P0017.17 §9：历史 run ⇒ 用该 run 的**持久化事实**画图并定位（只读；缺失事实如实标注）
      const candles = ((payload.candles || {}).candles) || [];
      // run 的标的必须来自 run identity，**不得**硬编码（否则别的标的的 run 会被贴错标签）
      let runSymbol = "UNKNOWN";
      try {
        const identity = await fetchJson(`${ENDPOINTS.runs}/${encodeURIComponent(runId)}`);
        runSymbol = String(((identity.run || {}).runtime || {}).symbol || "UNKNOWN");
      } catch (error) {
        runSymbol = "UNKNOWN";
      }
      lastRunReview = { runId: payload.run_id || runId, candles, facts: payload.facts || [],
                        symbol: runSymbol,
                        atrPoints: (((payload.indicators || {}).atr || {}).points || []) };
      const factRows = (payload.facts || []).slice(-60).reverse().map((item) => [
        String(item.ts), escapeHtml(String(item.kind || "")),
        escapeHtml(String(item.decision_id || item.client_order_id || "")),
        escapeHtml(String(item.status || item.mode || "")),
        `<button data-locate="${escapeHtml(String(item.ts))}">locate on chart</button>`]);
      return header +
        `<section class="wide"><h2>Recorded market chart (durable facts) · ${escapeHtml(runSymbol)} · ${escapeHtml(runId)}</h2>` +
        `<div id="wb-toolbar-runreview" class="wb-toolbar"></div>` +
        `<div id="kline-chart-runreview" class="kline-chart"></div>` +
        `<div class="muted">source=<code>durable</code> · ${escapeHtml(String(payload.note || ""))}</div></section>` +
        section("Run facts (locate on chart)", factRows.length
          ? table(["ts", "kind", "identity", "state", "locate"], factRows)
          : rows([["facts", '<span class="unknown">UNKNOWN（该 run 未记录 decision/order 事实）</span>']])) +
        section("Recorded market timeline", featurePanels(payload.timeline)) +
        section("Run review notes", rows([
          ["source", "durable（该 run 的持久化事实，不是当前接线缓冲）"],
          ["volume", (payload.trades > 0
            ? `<span class="known">${payload.trades} 笔持久化成交</span> <span class="muted">OHLCV 与服务端 VWAP 由这些真实成交聚合</span>`
            : '<span class="unknown">UNAVAILABLE</span> <span class="muted">该 run 未持久化逐笔成交 ⇒ 不伪造成交量</span>')],
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

  // Market View 分支：查看**非 runtime 交易标的**时，用公开行情研究视图（真实 REST，只读）。
  // 这不修改 Runtime Trading Symbol，也不重启运行实例、不触发任何交易动作。
  const runtimeSymbol = normalizeSymbol((await fetchSnapshot()).runtime.symbol);
  const viewSymbol = normalizeSymbol(hashQuery().get("view") || "");
  if (viewSymbol && viewSymbol !== runtimeSymbol) {
    // 渐进渲染：不阻塞页面；数据由 mount() 异步填充。
    lastMarket = null;
    pendingPublicView = { symbol: viewSymbol, runtimeSymbol };
    publicInterval = "1m";
    lastPublicView = null;
    return header + publicMarketViewShell(viewSymbol, runtimeSymbol);
  }
  // 回到 runtime 交易标的视图：清掉公开视图状态（否则 mount() 会挂错图表）
  pendingPublicView = null;
  lastPublicView = null;

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
      ${symbolSelectorHtml({ viewSymbol: runtimeSymbol, runtimeSymbol, sameAsRuntime: true })}
      <div class="row"><span class="k">other symbols</span><span class="v muted">
        <a href="#/market/universe">Market → universe</a>：多标的采集/落盘视图（需 runtime 接线多标的数据平台；
        本运行未接线时为 UNAVAILABLE）。要看/交易别的标的，用 \`--symbol &lt;SYMBOL&gt;\` 重启产品，
        并使用覆盖该标的的行情源（真实公网源会自动取该标的价格精度）。</span></div>
      <div id="chart-toolbar" class="toolbar"></div>
      <div id="kline-chart" class="chart-host" style="height:460px"></div>
      <div class="row"><span class="k">candles source</span><span class="v">${escapeHtml((candles1m.candles || {}).source || "unknown")}</span>
        <span class="k">note</span><span class="v">${escapeHtml((candles1m.candles || {}).note || "")}</span></div>
      <div class="row"><span class="k">semantic markers</span><span class="v">点击 K 线上的 prediction / decision / risk / order / ack / fill / cancel / reconciliation 标记 → Activity / Evidence / Raw Facts</span></div>
    </section>`;

  const body = chart +
    `<section><h2>Order book / microstructure</h2>${orderBookRows(depth)}</section>` +
    `<section><h2>Recent trades</h2><div data-recent-trades>${tradesTable(trades.trades)}</div></section>` +
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
let lastPublicView = null;
let pendingPublicView = null;
//: 公开视图当前周期（周期刷新与时间周期切换都用它，避免被重置回 1m）
let publicInterval = "1m";
let liveWorkbench = null;
let liveTimeframe = "1m";

/**
 * 公开行情研究视图外壳（**渐进渲染**）：先立即返回选择器 + 加载占位，
 * 由 `mount()` 异步取真实行情后填充，避免整个页面被上游请求阻塞（交易所限流时可达数秒）。
 */
function publicMarketViewShell(viewSymbol, runtimeSymbol) {
  const selector = symbolSelectorHtml({ viewSymbol, runtimeSymbol, sameAsRuntime: false });
  return `<section class="wide">
      <h2>Market view · ${escapeHtml(viewSymbol)} · public REST (research)</h2>
      ${selector}
      <div class="row"><span class="k">scope</span><span class="v muted">
        这是**公开行情研究视图**：不修改运行实例的交易标的，也不代表该标的在当前运行中被交易。</span></div>
      <div class="row"><span class="k">load state</span><span class="v" id="mv-status">loading…</span></div>
      <div id="mv-chart-host"></div>
    </section>` +
    section("Public market facts", '<div id="mv-facts"></div>') +
    section("Order book (snapshot)", '<div id="mv-book"><span class="muted">loading…</span></div>') +
    section("Recent trades", '<div id="mv-trades"><span class="muted">loading…</span></div>') +
    `<section class="wide"><h2>L2 depth heatmap</h2>` +
      `<div class="row"><span class="k">heatmap</span><span class="v unknown">NOT_AVAILABLE</span></div>` +
      `<div class="muted">${escapeHtml(L2_HEATMAP_REASON)}</div></section>` +
    section("Runtime context (unchanged)", rows([
      ["runtime trading symbol", `${escapeHtml(runtimeSymbol)} <span class="muted">（本视图为研究上下文，未受影响）</span>`],
      ["orders / positions / decisions", `<a href="#/activity/orders">Activity → orders</a> ` +
        `<span class="muted">（只属于 runtime 标的；不会因为查看 ${escapeHtml(viewSymbol)} 而归到它名下）</span>`],
      ["run / risk state", `<a href="#/monitor">Monitor</a> · <a href="#/system/risk">System → risk</a>`],
      ["quotes / decisions / prediction", '<span class="unknown">NOT_AVAILABLE in this view</span> ' +
        '<span class="muted">（这些由运行实例的 FeatureEngine/MakerPolicy 产生，只对 runtime 标的成立；' +
        '本视图只提供公开行情研究数据）</span>'],
      ["l2 heatmap / 连续深度序列", '<span class="unknown">NOT_AVAILABLE</span> ' +
        `<span class="muted">${escapeHtml(L2_HEATMAP_REASON)}</span>`],
    ]));
}

/** 异步加载并填充公开行情视图（由 mount 调用；失败只影响该区域并显示原因）。 */
async function mountPublicMarketView() {
  const request = pendingPublicView;
  if (!request) return;
  const status = document.getElementById("mv-status");
  const setStatus = (html) => { if (status) status.innerHTML = html; };
  let payload;
  try {
    payload = await fetchJson(`${ENDPOINTS.marketView}?symbol=${encodeURIComponent(request.symbol)}` +
      `&interval=${encodeURIComponent(publicInterval)}&candles=300&depth=50&trades=100`);
  } catch (error) {
    setStatus(`<span class="bad">UNAVAILABLE (${escapeHtml(String(error))})</span> ` +
      `<span class="muted">检查交易对是否受支持；若是交易所限流（HTTP 418/429）请稍后重试；` +
      `查看运行实例标的：<a href="#/market/live">${escapeHtml(request.runtimeSymbol)}</a></span>`);
    const facts = document.getElementById("mv-facts");
    if (facts) facts.innerHTML = rows([["public market data",
      `<span class="unknown">UNKNOWN</span> <span class="muted">（请求未被服务，不展示任何占位数）</span>`]]);
    return;
  }
  const view = payload.market_view || {};
  lastPublicView = { symbol: view.symbol || request.symbol,
                     candles: view.candles || [], interval: view.interval || publicInterval };
  const available = view.available || {};
  const errors = view.errors || {};
  setStatus(`<span class="known">loaded</span> <span class="muted">source=` +
    `${escapeHtml(view.source || "unknown")} · fetched_at=${escapeHtml(String(view.fetched_at_ms))}</span>`);
  const facts = document.getElementById("mv-facts");
  if (facts) facts.innerHTML = rows(marketViewFacts(view));
  const depth = view.depth || {};
  const bookHost = document.getElementById("mv-book");
  if (bookHost) {
    bookHost.innerHTML = available.depth
      ? table(["side", "price", "qty"],
          (depth.bids || []).slice(0, 12).map(([p, q]) => ["bid", escapeHtml(String(p)), escapeHtml(String(q))])
            .concat((depth.asks || []).slice(0, 12).map(([p, q]) => ["ask", escapeHtml(String(p)), escapeHtml(String(q))])))
      : `<div class="row"><span class="k">order book</span><span class="v unknown">UNAVAILABLE (` +
        `${escapeHtml((errors.depth || {}).code || "unknown")})</span></div>`;
  }
  const tradesHost = document.getElementById("mv-trades");
  if (tradesHost) {
    tradesHost.innerHTML = available.trades
      ? table(["ts", "price", "qty", "aggressor"], (view.trades || []).slice(0, 20).map((t) =>
          [String(t.ts), escapeHtml(String(t.price)), escapeHtml(String(t.quantity)),
           escapeHtml(String(t.aggressor))]))
      : `<div class="row"><span class="k">recent trades</span><span class="v unknown">UNAVAILABLE (` +
        `${escapeHtml((errors.trades || {}).code || "unknown")})</span></div>` +
        `<div class="muted">交易所限流/反爬导致该能力不可用；缺失不会被当成零成交。</div>`;
  }
  const chartHost = document.getElementById("mv-chart-host");
  if (chartHost) {
    chartHost.innerHTML = available.candles
      ? `<div id="mv-toolbar" class="toolbar"></div>` +
        `<div id="mv-kline-chart" class="chart-host" style="height:420px"></div>`
      : `<div class="row"><span class="k">candles</span><span class="v unknown">UNAVAILABLE (` +
        `${escapeHtml((errors.candles || {}).code || "unknown")})</span></div>`;
  }
  if ((lastPublicView.candles || []).length) {
    lastPublicView.workbench = mountWorkbench(
      document.getElementById("mv-kline-chart"), document.getElementById("mv-toolbar"), {
        candles: lastPublicView.candles, trace: [], timeframe: lastPublicView.interval,
        symbol: lastPublicView.symbol, source: "public_rest (read-only)",
        indicators: ["MA", "EMA", "VOL"],
        // 时间周期切换必须**重新取该周期的真实 K 线**（此前只换标签不换数据）。
        onTimeframe: (interval) => { void refreshPublicCandles(String(interval)); },
      });
  }
}

/** 公开视图：按所选周期重新取真实 K 线并刷新图表与事实区块。 */
async function refreshPublicCandles(interval) {
  const request = pendingPublicView;
  if (!request) return;
  const status = document.getElementById("mv-status");
  if (status) status.innerHTML = `<span class="unknown">loading ${escapeHtml(interval)}…</span>`;
  try {
    publicInterval = String(interval);
    const payload = await fetchJson(`${ENDPOINTS.marketView}?symbol=` +
      `${encodeURIComponent(request.symbol)}&interval=${encodeURIComponent(interval)}` +
      "&components=candles&candles=300");
    const view = payload.market_view || {};
    const candles = view.candles || [];
    lastPublicView = { ...(lastPublicView || {}), symbol: view.symbol || request.symbol,
                       candles, interval };
    if (lastPublicView.workbench) {
      lastPublicView.workbench.refresh({ candles, trace: [], source: view.source });
    }
    const facts = document.getElementById("mv-facts");
    if (facts && (view.available || {}).candles) {
      const candleRow = Array.from(facts.querySelectorAll(".row"))
        .find((row) => /candles/i.test(row.innerText));
      if (candleRow) {
        candleRow.innerHTML = `<span class="k">candles</span><span class="v">` +
          `${candles.length} 根 · interval=${escapeHtml(interval)} ` +
          `<span class="muted">(source=${escapeHtml(view.source || "")}, ` +
          `fetched_at=${escapeHtml(String(view.fetched_at_ms))})</span></span>`;
      }
    }
    if (status) status.innerHTML = `<span class="known">loaded ${escapeHtml(interval)}</span> ` +
      `<span class="muted">fetched_at=${escapeHtml(String(view.fetched_at_ms))}</span>`;
  } catch (error) {
    if (status) status.innerHTML = `<span class="unknown">candles ${escapeHtml(interval)} ` +
      `UNAVAILABLE (${escapeHtml(String(error))})</span>`;
  }
}

export async function mount(rest = []) {
  // Market View（公开行情研究视图）：选择器立即可用，行情异步填充（渐进渲染）
  if (pendingPublicView) {
    const request = pendingPublicView;
    mountSymbolSelector();
    await mountPublicMarketView();
    if (!lastPublicView) lastPublicView = { symbol: request.symbol, candles: [], interval: "1m" };
    return;
  }
  // P0001.17 §9：Run Review 的记录事实图表（历史 run；只读展示 + 事实定位）
  if (rest[0] === "run-review" && lastRunReview && lastRunReview.candles.length) {
    const workbench = mountWorkbench(document.getElementById("kline-chart-runreview"),
                                    document.getElementById("wb-toolbar-runreview"), {
      candles: lastRunReview.candles, trace: [], timeframe: "1m",
      symbol: lastRunReview.symbol || "UNKNOWN",
      atrPoints: lastRunReview.atrPoints || [],
      source: "durable run facts", indicators: ["MA", "EMA", "VOL"],
    });
    document.querySelectorAll("button[data-locate]").forEach((button) => {
      button.addEventListener("click", () => workbench && workbench.setFocus(Number(button.dataset.locate)));
    });
    updateSelection({ surface: "market", run: lastRunReview.runId, timestamp: null,
                      decision: null, order: null, fill: null });
  }
  const context = lastMarket;
  if (context) mountSymbolSelector();
  if (!context) return;
  const { candles1m, trace, focusTs, depth, symbol } = context;
  const snapshot = await fetchSnapshot();

    const canvas = document.getElementById("heatmap");
    if (canvas) drawHeatmap(canvas, depth.depth);

    let timeframe = "1m";
    liveTimeframe = "1m";
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
      liveTimeframe = interval;
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
    liveWorkbench = workbench;

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

/**
 * 非破坏实时刷新（console surface poll 调用）：在不重建图表、保留用户画线的前提下更新数据。
 * 只在 live 视图有效；run-review / 无 workbench 时返回（让 console 回退整体渲染）。
 */
export async function refresh(rest = []) {
  const active = rest[0] && VIEWS.includes(rest[0]) ? rest[0] : "live";
  if (active === "universe") {
    const host = document.getElementById("view");
    if (!host) return;
    host.innerHTML = rest[1]
      ? await renderSymbolDetail(String(rest[1]))
      : await renderUniverse();
    return;
  }
  // 公开行情研究视图：轮询只刷新 **K 线**（单次请求）；盘口/成交/标记价/概览保持初次加载结果，
  // 避免每 5s 连打 5 个上游端点触发交易所限流（各自 fetched_at 已显示，用户可重新选择交易对刷新）。
  if (pendingPublicView) {
    await refreshPublicCandles(publicInterval);
    return;
  }
  if (!liveWorkbench) return;
  if (active !== "live") return;
  const interval = liveTimeframe;
  const [series, tracePayload, snapshot] = await Promise.all([
    fetchJson(`${ENDPOINTS.marketCandles}?interval=${interval}&limit=300`),
    fetchJson(ENDPOINTS.evidence), fetchSnapshot(),
  ]);
  liveWorkbench.refresh({
    candles: (series.candles || {}).candles || [],
    trace: (tracePayload.evidence || {}).trace || [],
    source: (series.candles || {}).source,
  });
  // 次要面板（order book/trades/heatmap/prediction）：轻量更新有容器的部分
  try {
    const trades = await fetchJson(ENDPOINTS.marketTrades);
    const tradesHost = document.querySelector("[data-recent-trades]");
    if (tradesHost) tradesHost.innerHTML = tradesTable(trades.trades);
  } catch (error) { /* best effort */ }
  const predictionHost = document.getElementById("market-prediction");
  if (predictionHost && snapshot.prediction) {
    predictionHost.innerHTML = predictionSection(snapshot.prediction);
    mountPredictionPanel(document.getElementById("prediction-horizons"), snapshot.prediction);
  }
}

