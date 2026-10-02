/**
 * 真实浏览器截图（Chrome headless + CDP；只用 Node 内置 fetch/WebSocket，不引入依赖）。
 *
 * 用途：验收「浏览器真实可见、截图可审」。对每个页面真实导航、等待渲染，必要时用真实鼠标
 * 事件操作图表（画线工具），然后 Page.captureScreenshot。
 *
 * 用法：node tests/ui/capture.mjs <base-url> <out-dir>
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [base, outDir] = [process.argv[2], process.argv[3] || "/tmp/probex_ui/shots"];
const PORT = 9333 + Math.floor(Math.random() * 200);
mkdirSync(outDir, { recursive: true });

const chrome = spawn(CHROME, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${join(tmpdir(), `probex-chrome-${PORT}`)}`,
  "--no-first-run", "--no-default-browser-check", "--no-sandbox", "--disable-gpu",
  "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1680,1050", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function endpoint(path) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}${path}`);
      if (response.ok) return await response.json();
    } catch (error) { /* not up yet */ }
    await sleep(250);
  }
  throw new Error(`chrome endpoint ${path} not ready`);
}

class Page {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async open(url) {
    const target = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`,
                               { method: "PUT" }).then((r) => r.json());
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
    const page = new Page(ws);
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && page.pending.has(message.id)) {
        const { resolve, reject } = page.pending.get(message.id);
        page.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
      }
    };
    await page.send("Page.enable");
    await page.send("Runtime.enable");
    await page.send("Emulation.setDeviceMetricsOverride",
                    { width: 1680, height: 1050, deviceScaleFactor: 1, mobile: false });
    return page;
  }
  send(method, params = {}) {
    const id = (this.id += 1);
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    return result.result ? result.result.value : null;
  }
  async goto(url, waitMs = 2500) { await this.send("Page.navigate", { url }); await sleep(waitMs); }
  async mouse(x, y, type) {
    await this.send("Input.dispatchMouseEvent",
                    { type, x, y, button: "left", buttons: type === "mouseReleased" ? 0 : 1, clickCount: 1 });
  }
  async click(x, y) {
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, buttons: 0 });
    await sleep(60);
    await this.mouse(x, y, "mousePressed"); await sleep(90); await this.mouse(x, y, "mouseReleased");
    await sleep(320);
  }
  async shot(name) {
    const result = await this.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    const file = join(outDir, `${name}.png`);
    writeFileSync(file, Buffer.from(result.data, "base64"));
    return file;
  }
}

async function chartRect(page) {
  return page.evaluate(`(() => { const el = document.getElementById('kline-chart');
    if (!el) return null; const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height }; })()`);
}

const results = [];
try {
  await endpoint("/json/version");
  const page = await Page.open(`${base}/#/monitor`);
  await sleep(2500);

  // 1. Monitor
  results.push(await page.shot("01-monitor"));

  // 2. Market K-line
  await page.goto(`${base}/#/market/live`, 3500);
  results.push(await page.shot("02-market-kline"));

  // 3. Market drawing tools（真实鼠标点一次画水平线 + 用库 API 补齐 rect/fibonacci，使截图可审）
  const rect = await chartRect(page);
  if (rect) {
    await page.evaluate(`document.querySelector('button[data-draw="horizontalStraightLine"]').click()`);
    await sleep(300);
    await page.click(rect.x + rect.w * 0.5, rect.y + rect.h * 0.45);   // 真实鼠标交互
    await page.evaluate(`(() => { const a = document.getElementById('kline-chart').__probex;
      const l = a.chart.getDataList();
      const p1 = { timestamp: l[20].timestamp, value: l[20].high * 1.004 };
      const p2 = { timestamp: l[70].timestamp, value: l[70].low * 0.996 };
      a.chart.createOverlay({ name: 'rect', points: [p1, p2] });
      a.chart.createOverlay({ name: 'fibonacciSegment', points: [p1, p2] });
      a.chart.createOverlay({ name: 'segment', points: [p1, p2] });
      a.refreshStatus(); })()`);
    await sleep(600);
  }
  results.push(await page.shot("03-market-drawings"));

  // 4. K-line semantic markers（先离开再回来，确保得到干净的一次渲染）
  await page.goto(`${base}/#/activity`, 1200);
  await page.goto(`${base}/#/market/live`, 3500);
  await page.evaluate("window.dispatchEvent(new Event('resize'))");
  results.push(await page.shot("04-market-semantic-markers"));

  // 5. L2 heatmap（滚动到 heatmap）
  await page.evaluate("document.getElementById('heatmap') && document.getElementById('heatmap').scrollIntoView({block:'center'})");
  await sleep(600);
  results.push(await page.shot("05-market-l2-heatmap"));

  // 6. Activity strategy / causal chain
  await page.goto(`${base}/#/activity`, 3000);
  results.push(await page.shot("06-activity-causal-chain"));

  // 7. Prediction panel（Activity 底部 prediction host）
  await page.evaluate("const el=document.getElementById('activity-prediction'); el && el.scrollIntoView({block:'center'})");
  await sleep(700);
  results.push(await page.shot("07-prediction-panel"));

  // 8. Performance（equity chart + compare）
  await page.goto(`${base}/#/performance`, 3500);
  results.push(await page.shot("08-performance-charts"));

  // 9. System
  await page.goto(`${base}/#/system/ops`, 3000);
  results.push(await page.shot("09-system-ops"));

  // 10. Assistant drawer（真实点击一个建议动作）
  await page.goto(`${base}/#/market/live`, 3500);
  await page.evaluate(`(() => { const b = document.querySelector('#assistant-actions button[data-action]');
    if (b) b.click(); })()`);
  await sleep(1200);
  results.push(await page.shot("10-assistant-drawer"));
  page.ws.close();
} finally {
  chrome.kill("SIGKILL");
}
console.log(JSON.stringify({ shots: results }, null, 1));
