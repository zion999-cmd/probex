/**
 * Probex Market K-line workbench.
 *
 * 复用的轮子：klinecharts（candlestick / zoom / pan / crosshair / tooltip / axis / indicators /
 * drawing overlays）+ @klinecharts/extension（rect / arrow / measure / fibonacci*）。
 * Probex 自己只实现：data adapter、semantic overlays、canonical identity navigation、
 * replay 同步、selection → Assistant 上下文。
 */

import * as klinecharts from "klinecharts";   // import map → /ui/vendor/klinecharts-shim.js (UMD 桥接)
import { arrow, fibonacciExtension, fibonacciSegment, measure, rect } from "/vendor/@klinecharts/extension/dist/index.js";
import { PROBEX_INDICATORS, atrPointsFromBackend, overlayPointsFromBackend, registerProbexIndicators } from "/ui/pages/market/indicators.js";
import { registerFactIndicators, registerProbexOverlays, semanticOverlays } from "/ui/pages/market/semantic_overlays.js";
import { entityHash } from "/ui/app/navigation.js";

export const TIMEFRAMES = ["1m", "5m", "15m", "1h"];
const PERIODS = { "1m": { type: "minute", span: 1 }, "5m": { type: "minute", span: 5 },
                  "15m": { type: "minute", span: 15 }, "1h": { type: "hour", span: 1 } };
//: 直接复用库/扩展的 drawing overlays（不自造几何引擎）
export const DRAWING_TOOLS = [
  { name: "horizontalStraightLine", label: "水平线" },
  { name: "segment", label: "趋势线" },
  { name: "rect", label: "矩形/区间" },
  { name: "arrow", label: "箭头/标注" },
  { name: "measure", label: "测量" },
  { name: "fibonacciSegment", label: "斐波那契回撤" },
  { name: "fibonacciExtension", label: "斐波那契扩展" },
];

const DARK_STYLES = {
  grid: { horizontal: { color: "#20242e" }, vertical: { color: "#20242e" } },
  candle: {
    bar: { upColor: "#2ea043", downColor: "#e5534b", upBorderColor: "#2ea043",
           downBorderColor: "#e5534b", upWickColor: "#2ea043", downWickColor: "#e5534b" },
    tooltip: { text: { color: "#d7dae0", size: 11 } },
  },
  indicator: { lines: [{ color: "#7aa2f7" }, { color: "#e0af68" }, { color: "#bb9af7" }],
               tooltip: { text: { color: "#d7dae0", size: 11 } } },
  xAxis: { axisLine: { color: "#2a2f3a" }, tickLine: { color: "#2a2f3a" },
           tickText: { color: "#8b93a1", size: 10 } },
  yAxis: { axisLine: { color: "#2a2f3a" }, tickLine: { color: "#2a2f3a" },
           tickText: { color: "#8b93a1", size: 10 } },
  crosshair: { horizontal: { line: { color: "#8b93a1" }, text: { backgroundColor: "#39404e" } },
               vertical: { line: { color: "#8b93a1" }, text: { backgroundColor: "#39404e" } } },
  overlay: { text: { color: "#d7dae0", size: 11 } },
};

function toKLine(candles) {
  return (candles || []).map((bar) => ({
    timestamp: Number(bar.ts), open: Number(bar.open), high: Number(bar.high),
    low: Number(bar.low), close: Number(bar.close),
    volume: Number.isFinite(Number(bar.volume)) ? Number(bar.volume) : 0,
  }));
}

/** 给定时间戳 → 该时刻（或之前最近一根）的收盘价；无数据 ⇒ null。 */
export function priceAt(candles, timestamp) {
  let best = null;
  for (const bar of candles || []) {
    if (Number(bar.ts) <= Number(timestamp)) best = bar; else break;
  }
  if (best === null && candles && candles.length) best = candles[0];
  return best === null ? null : Number(best.close);
}

export function toolbarHtml({ timeframe = "1m", indicators = ["MA", "VOL"] } = {}) {
  const tf = TIMEFRAMES.map((item) =>
    `<button data-timeframe="${item}" class="${item === timeframe ? "active" : ""}">${item}</button>`).join("");
  const ind = PROBEX_INDICATORS.map((item) =>
    `<button data-indicator="${item.name}" class="${indicators.includes(item.name) ? "active" : ""}">${item.name}</button>`).join("");
  const draw = DRAWING_TOOLS.map((item) =>
    `<button data-draw="${item.name}" title="${item.name}">${item.label}</button>`).join("");
  return `
    <div class="wb-group"><span class="wb-label">timeframe</span>${tf}</div>
    <div class="wb-group"><span class="wb-label">indicators</span>${ind}</div>
    <div class="wb-group"><span class="wb-label">draw</span>${draw}
      <button data-draw-clear="1">清空</button></div>
    <div class="wb-group"><span class="wb-label" id="wb-status">ready</span></div>`;
}

/** 挂载 workbench（真实 klinecharts）。无库/无容器 ⇒ 返回 null（由页面渲染 fallback）。 */
export function mountWorkbench(chartHost, toolbarHost, options = {}) {
  if (typeof klinecharts === "undefined" || !klinecharts || typeof klinecharts.init !== "function") return null;
  if (!chartHost || !toolbarHost) return null;

  registerProbexIndicators(klinecharts);
  registerProbexOverlays(klinecharts);
  registerFactIndicators(klinecharts);
  for (const template of [rect, arrow, measure, fibonacciSegment, fibonacciExtension]) {
    try { klinecharts.registerOverlay(template); } catch (error) { /* 已注册则忽略 */ }
  }

  let candles = (options.candles || []).slice();
  let trace = options.trace || [];
  let timeframe = options.timeframe || "1m";
  const activeIndicators = new Set(options.indicators || ["MA", "VOL"]);
  const statusEl = () => toolbarHost.querySelector("#wb-status");

  toolbarHost.innerHTML = toolbarHtml({ timeframe, indicators: [...activeIndicators] });
  const chart = klinecharts.init(chartHost, { styles: DARK_STYLES, locale: "zh-CN" });
  chart.setSymbol({ ticker: options.symbol || "BTCUSDT", pricePrecision: 2, volumePrecision: 4 });
  chart.setPeriod(PERIODS[timeframe] || PERIODS["1m"]);
  chart.setStyles(DARK_STYLES);

  const notify = (selection) => { if (typeof options.onSelection === "function") options.onSelection(selection); };

  function refreshStatus(note) {
    const status = statusEl();
    if (!status) return;
    const drawings = DRAWING_TOOLS.reduce((sum, tool) => sum + chart.getOverlays({ name: tool.name }).length, 0);
    const markers = chart.getOverlays({ name: "probexSemantic" }).length;
    status.textContent = `${candles.length} candles · ${trace.length} trace · ${markers} markers · ` +
      `${drawings} drawings` + (note ? ` · ${note}` : "") + (options.source ? ` · ${options.source}` : "");
  }

  // 鼠标松开后刷新状态（画线是真实用户交互；这里只做可观察计数）
  chartHost.addEventListener("mouseup", () => setTimeout(() => refreshStatus(), 0));

  function applyData() {
    chart.setDataLoader({
      getBars: ({ type, callback }) => {
        if (type === "init") callback(toKLine(candles));
        else callback([], false);
      },
    });
    // 语义 overlay（canonical id + ts）
    try { chart.removeOverlay({ name: "probexSemantic" }); } catch (error) { /* 无则忽略 */ }
    const overlays = semanticOverlays(trace, (ts) => priceAt(candles, ts), entityHash);
    if (overlays.length) chart.createOverlay(overlays);
    // P0001.17 §5：VWAP / ATR 来自**后端事实**（不在此计算）——有事实才画线，否则由页面显示 NOT_AVAILABLE
    try {
      chart.removeOverlay({ name: "probexVwap" });
      chart.removeOverlay({ name: "probexAtr" });
    } catch (error) { /* 无则忽略 */ }
    const vwapPoints = overlayPointsFromBackend(candles, "vwap");
    if (vwapPoints.length) {
      chart.createOverlay({ name: "probexVwap", points: vwapPoints });
    }
    const atrPoints = atrPointsFromBackend(options.atrPoints);
    if (atrPoints.length) {
      chart.createOverlay({ name: "probexAtr", points: atrPoints });
    }
    const focus = options.focusTs;
    if (focus) chart.scrollToTimestamp(Number(focus), 0);
    refreshStatus();
  }

  function syncIndicators() {
    for (const item of PROBEX_INDICATORS) {
      const active = activeIndicators.has(item.name);
      const existing = chart.getIndicators({ name: item.name });
      if (active && existing.length === 0) chart.createIndicator({ name: item.name });
      if (!active && existing.length > 0) chart.removeIndicator({ name: item.name });
    }
  }

  applyData();
  syncIndicators();

  chart.subscribeAction("onCrosshairChange", (crosshair) => {
    if (!crosshair) return;
    notify({ type: "crosshair", timestamp: crosshair.timestamp || null, timeframe,
             candle: crosshair.kLineData || null });
  });
  chart.subscribeAction("onCandleBarClick", (data) => {
    if (data && data.timestamp) notify({ type: "candle", timestamp: data.timestamp, timeframe });
  });

  // toolbar bindings
  toolbarHost.querySelectorAll("button[data-timeframe]").forEach((button) => {
    button.addEventListener("click", () => {
      timeframe = button.dataset.timeframe;
      toolbarHost.querySelectorAll("button[data-timeframe]").forEach((item) =>
        item.classList.toggle("active", item === button));
      if (typeof options.onTimeframe === "function") options.onTimeframe(timeframe);
    });
  });
  toolbarHost.querySelectorAll("button[data-indicator]").forEach((button) => {
    button.addEventListener("click", () => {
      const name = button.dataset.indicator;
      if (activeIndicators.has(name)) activeIndicators.delete(name); else activeIndicators.add(name);
      button.classList.toggle("active", activeIndicators.has(name));
      syncIndicators();
    });
  });
  toolbarHost.querySelectorAll("button[data-draw]").forEach((button) => {
    button.addEventListener("click", () => {
      const name = button.dataset.draw;
      chart.createOverlay({ name });
      refreshStatus(`drawing ${name}`);
    });
  });
  const clear = toolbarHost.querySelector("button[data-draw-clear]");
  if (clear) {
    clear.addEventListener("click", () => {
      for (const tool of DRAWING_TOOLS) chart.removeOverlay({ name: tool.name });
      refreshStatus("drawings cleared (semantic markers kept)");
    });
  }

  const onResize = () => chart.resize();
  window.addEventListener("resize", onResize);

  // 浏览器验收钩子（只读 + 既有库 API；不新增图表能力）
  const api = {
    chart,
    candles: () => candles.length,
    markers: () => chart.getOverlays({ name: "probexSemantic" }).length,
    drawings: () => DRAWING_TOOLS.reduce((sum, tool) => sum + chart.getOverlays({ name: tool.name }).length, 0),
    indicators: () => chart.getIndicators().map((item) => item.name),
    refreshStatus,
  };
  if (chartHost) chartHost.__probex = api;

  return Object.assign(api, {
    refresh(payload = {}) {
      candles = (payload.candles || candles).slice();
      trace = payload.trace || trace;
      options.candles = candles; options.trace = trace;
      if (payload.focusTs !== undefined) options.focusTs = payload.focusTs;
      if (payload.source) options.source = payload.source;
      applyData();
    },
    setFocus(timestamp) { if (timestamp) chart.scrollToTimestamp(Number(timestamp), 0); },
    drawingCount() {
      return DRAWING_TOOLS.reduce((sum, tool) => sum + chart.getOverlays({ name: tool.name }).length, 0);
    },
    destroy() { window.removeEventListener("resize", onResize); try { klinecharts.dispose(chartHost); } catch (error) { /* noop */ } },
  });
}
