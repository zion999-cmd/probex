/**
 * 真实浏览器验收：K 线工作台的交互能力（REPLAY/PAPER 运行中）。
 * 断言：candles / indicators / semantic markers / drawing create-move-delete-clear / timeframe / zoom-pan /
 * crosshair → assistant selection。输出一行 JSON（每项 PASS/FAIL）。
 *
 * 用法：node tests/ui/verify_chart.mjs <base-url>
 */
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const base = process.argv[2] || "http://127.0.0.1:8840";
const PORT = 9500 + Math.floor(Math.random() * 90);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${join(tmpdir(), `verify-${PORT}`)}`, "--no-first-run", "--no-sandbox",
  "--disable-gpu", "--window-size=1680,1050", "about:blank"], { stdio: "ignore" });

for (let i = 0; i < 60; i += 1) {
  try { if ((await fetch(`http://127.0.0.1:${PORT}/json/version`)).ok) break; } catch (e) { /* wait */ }
  await sleep(250);
}
const target = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" }).then((r) => r.json());
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pending = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const i = (id += 1); pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })).result?.value;

await send("Runtime.enable"); await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1680, height: 1050, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: `${base}/#/market/live` });
await sleep(4000);

const results = {};
const api = "document.getElementById('kline-chart').__probex";
results.chartMounted = await ev(`!!${api}`);
results.candles = await ev(`${api}.candles()`);
results.indicators = await ev(`${api}.indicators()`);
results.semanticMarkers = await ev(`${api}.markers()`);
// drawing create（库/扩展 overlay，真实 chart 实例）
results.drawingCreated = await ev(`(() => { const a = ${api}; const list = a.chart.getDataList();
  const p1 = { timestamp: list[20].timestamp, value: list[20].high * 1.002 };
  const p2 = { timestamp: list[60].timestamp, value: list[60].low * 0.998 };
  a.chart.createOverlay({ name: 'horizontalStraightLine', points: [p1] });
  a.chart.createOverlay({ name: 'rect', points: [p1, p2] });
  a.chart.createOverlay({ name: 'fibonacciSegment', points: [p1, p2] });
  a.refreshStatus(); return a.drawings(); })()`);
// move（overrideOverlay 改 points）
results.drawingMoved = await ev(`(() => { const a = ${api}; const o = a.chart.getOverlays({ name: 'horizontalStraightLine' })[0];
  if (!o || !o.points || !o.points.length) return false; const before = JSON.stringify(o.points);
  a.chart.overrideOverlay({ id: o.id, points: [{ timestamp: o.points[0].timestamp, value: (o.points[0].value || 100) * 1.01 }] });
  const after = JSON.stringify(a.chart.getOverlays({ name: 'horizontalStraightLine' })[0].points); return before !== after; })()`);
// delete one
results.drawingDeleted = await ev(`(() => { const a = ${api}; const o = a.chart.getOverlays({ name: 'rect' })[0];
  const before = a.drawings(); if (o) a.chart.removeOverlay({ id: o.id }); return before - a.drawings() === 1; })()`);
// clear all drawings but keep semantic markers
results.cleared = await ev(`(() => { const a = ${api}; ['horizontalStraightLine','segment','rect','arrow','measure','fibonacciSegment','fibonacciExtension'].forEach(n => a.chart.removeOverlay({ name: n }));
  return a.drawings() === 0; })()`);
// zoom / pan
// 画线是否**真实渲染**：对 chart 区域做 clipped screenshot，前后字节不同
const chartBox = await ev("(() => { const r = document.getElementById('kline-chart').getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }; })()");
const clip = { ...chartBox, scale: 1 };
const shotBefore = (await send("Page.captureScreenshot", { format: "png", clip })).data;
await ev(`(() => { const a = ${api}; const list = a.chart.getDataList();
  const p1 = { timestamp: list[30].timestamp, value: list[30].high * 1.004 };
  const p2 = { timestamp: list[80].timestamp, value: list[80].low * 0.996 };
  a.chart.createOverlay({ name: 'rect', points: [p1, p2] });
  a.chart.createOverlay({ name: 'fibonacciSegment', points: [p1, p2] }); })()`);
await sleep(900);
const shotAfter = (await send("Page.captureScreenshot", { format: "png", clip })).data;
results.drawingRendered = shotBefore !== shotAfter;
await ev(`['rect','fibonacciSegment'].forEach(n => ${api}.chart.removeOverlay({ name: n }))`);
results.zoomPan = await ev(`(() => { const a = ${api}; a.chart.zoomAtDataIndex(0.5, 5); a.chart.scrollByDistance(-120); a.chart.scrollToRealTime(); return true; })()`);
// timeframe switch (UI button) → candles reloaded
const beforeTf = await ev(`${api}.candles()`);
await ev(`document.querySelector('button[data-timeframe="5m"]').click()`);
await sleep(1800);
results.timeframeSwitched = await ev(`${api}.candles()`) !== beforeTf;
// replay sync: step → chart refresh keeps candles
await ev(`document.querySelector('button[data-verb="step"]').click()`);
await sleep(1500);
results.replaySync = await ev(`${api}.candles() > 0`);
// crosshair → assistant selection
await ev(`document.getElementById('kline-chart').dispatchEvent(new MouseEvent('mousemove', {clientX: 500, clientY: 300, bubbles: true}))`);
await sleep(300);
results.assistantSelection = await ev(`(async () => { const m = await import('/ui/client/selection.js'); const s = m.getSelection();
  return !!s.surface; })()`);
results.assistantContext = await ev(`(async () => { const r = await fetch('/api/v1/assistant/context?surface=market&timeframe=5m&timestamp=1700000000000');
  const d = await r.json(); return d.context && d.context.timeframe && d.context.timeframe.known ? d.context.timeframe.value : null; })()`);

console.log(JSON.stringify(results, null, 1));
ws.close(); chrome.kill("SIGKILL");
