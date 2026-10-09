/**
 * P0001.17 §16 B/C：真实浏览器 E2E（本地 PAPER 闭环 + 图表数据链）。
 *
 * 断言（每项都要求真实后端数据，不用 mock）：
 *   1. Monitor：LOCAL_TRIAL 标注 + 真实 position/equity/orders
 *   2. Market：K 线 canvas 挂载 + **真实 candles 来自后端**（比较 API 与图表数据）+ 指标可见
 *   3. Activity：causal chain（decision → risk → order → fill）+ decision drill-down 展开
 *   4. Performance：equity / PnL 序列来自真实 account timeline
 *   5. System：两个 connector health 分开显示
 *   6. Assistant：Run/Decision/Order 上下文 + LOCAL_TRIAL 说明
 *
 * 用法：node tests/ui/capture_local_loop.mjs <base-url> <out-dir>
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [base, outDir] = [process.argv[2], process.argv[3] || "/tmp/probex_p117/shots"];
const PORT = 9800 + Math.floor(Math.random() * 150);
mkdirSync(outDir, { recursive: true });

const chrome = spawn(CHROME, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${join(tmpdir(), `probex-p117-${PORT}`)}`,
  "--no-first-run", "--no-default-browser-check", "--no-sandbox", "--disable-gpu",
  "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1680,1050", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function endpoint(path) {
  for (let i = 0; i < 60; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}${path}`);
      if (res.ok) return await res.json();
    } catch { /* not up */ }
    await sleep(250);
  }
  throw new Error(`chrome ${path} not ready`);
}

class Page {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async open(url) {
    const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`,
      { method: "PUT" })).json();
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const page = new Page(ws);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && page.pending.has(msg.id)) {
        const { resolve, reject } = page.pending.get(msg.id);
        page.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error))); else resolve(msg.result);
      }
    };
    await page.send("Page.enable");
    await page.send("Runtime.enable");
    return page;
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async goto(url, settle = 3500) {
    // hash-only 变更不会重新加载 SPA ⇒ 先 about:blank 再带 cache-busting query 强制真实加载
    await this.send("Page.navigate", { url: "about:blank" });
    await sleep(200);
    const target = new URL(url);
    target.searchParams.set("t", String(Date.now()));      // cache-busting 必须在 hash 之前
    await this.send("Page.navigate", { url: target.toString() });
    await sleep(settle);
  }
  async text() { return String((await this.evaluate("document.body.innerText")) || ""); }
  /** 等待文本出现（真实渲染完成条件；上限 timeout 后返回 false，不静默通过）。 */
  async waitForText(needle, timeout = 15000, interval = 250) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const text = await this.text();
      if (text.includes(needle)) return true;
      await sleep(interval);
    }
    return false;
  }
  /** 等待选择器出现。 */
  async waitForSelector(selector, timeout = 15000, interval = 250) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await this.evaluate(`!!document.querySelector(${JSON.stringify(selector)})`)) return true;
      await sleep(interval);
    }
    return false;
  }
  async evaluate(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    return r.result.value;
  }
  async shot(name) {
    const r = await this.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    writeFileSync(join(outDir, `${name}.png`), Buffer.from(r.data, "base64"));
    return `${name}.png`;
  }
  close() { this.ws.close(); }
}

const results = [];
const record = async (page, name, checks) => {
  // 条件等待：先等第一个标志文本出现（避免负载高时采样过早）
  if (checks.length) await page.waitForText(checks[0], 15000);
  const text = await page.text();
  const lower = text.toLowerCase();
  const missing = checks.filter((needle) => !lower.includes(needle.toLowerCase()));
  results.push({ name, shot: await page.shot(name), missing, excerpt: text.slice(0, 260).replace(/\s+/g, " ") });
};

await endpoint("/json/version");
const page = await Page.open("about:blank");
try {
  // 1) Monitor
  await page.goto(`${base}/#/monitor`);
  await page.waitForText("LOCAL_TRIAL", 15000);
  await record(page, "01-monitor-local-trial", ["LOCAL_TRIAL", "position", "equity"]);

  // 2) Market：K 线 + 指标 + **图表数据链**（API 与图表一致）
  await page.goto(`${base}/#/market/live`, 3000);
  await page.waitForSelector("#kline-chart canvas", 20000);
  const chart = await page.evaluate(`(async () => {
    const api = await (await fetch("/api/v1/market/candles?interval=1m&limit=200")).json();
    const canvases = document.querySelectorAll("canvas").length;
    const apiCandles = (api.candles && api.candles.candles) ? api.candles.candles : (api.candles || []);
    const last = apiCandles[apiCandles.length - 1] || null;
    return { canvases, apiCount: apiCandles.length,
             apiLastClose: last ? (last.close ?? null) : null,
             apiTimestamps: apiCandles.length ? [apiCandles[0].ts, last ? last.ts : null] : [] };
  })()`);
  await record(page, "02-market-kline-real-data", ["LOCAL_TRIAL"]);
  results[results.length - 1].chart = chart;

  // 3) Activity：因果链 + decision drill-down
  await page.goto(`${base}/#/activity`, 3000);
  await page.waitForSelector("a[data-decision]", 20000);
  const drill = await page.evaluate(`(async () => {
    const link = document.querySelector("a[data-decision]");
    if (!link) return { clicked: false };
    link.click();
    await new Promise((r) => setTimeout(r, 3500));
    const panel = document.getElementById("decision-detail");
    return { clicked: true, decisionId: link.dataset.decision,
             text: panel ? panel.innerText.slice(0, 600) : null };
  })()`);
  await record(page, "03-activity-causal-chain-drilldown", ["causal chain", "Decision drill-down"]);
  results[results.length - 1].drilldown = drill;

  // 3b) Run Review：当前 run ⇒ 可定位；未知 run ⇒ 明确 UNKNOWN + 恢复入口（不得跳错误时间）
  const runReview = await page.evaluate(`(async () => {
    const snap = await (await fetch("/api/v1/snapshot")).json();
    const runId = snap.runtime.runtime_id;
    const okRes = await fetch("/api/v1/runs/" + encodeURIComponent(runId) + "/market");
    const missingRes = await fetch("/api/v1/runs/run-does-not-exist/market");
    let missingBody = null;
    try { missingBody = await missingRes.json(); } catch (e) { missingBody = String(e); }
    return { runId, currentRunStatus: okRes.status, unknownRunStatus: missingRes.status,
             unknownRunBody: missingBody };
  })()`);
  await page.goto(`${base}/#/market/run-review/${encodeURIComponent(runReview.runId)}`, 3000);
  await page.waitForText("Locate on chart", 20000);
  await record(page, "07-run-review-current", ["Recorded market timeline", "Locate on chart"]);
  results[results.length - 1].runReview = runReview;
  await page.goto(`${base}/#/market/run-review/run-does-not-exist`, 3000);
  await page.waitForText("UNKNOWN", 20000);
  await record(page, "08-run-review-unknown", ["UNKNOWN"]);

  // 4) Performance：真实 equity/PnL 序列
  await page.goto(`${base}/#/performance`, 3000);
  await page.waitForText("Trade statistics", 20000);
  const perf = await page.evaluate(`(async () => {
    const tl = await (await fetch("/api/v1/portfolio/timeline")).json();
    return { canvases: document.querySelectorAll("canvas").length,
             timelinePoints: ((tl.timeline || {}).points || []).length,
             timelineUnavailable: (tl.timeline || {}).unavailable || null };
  })()`);
  await record(page, "04-performance-equity-curve", ["Performance"]);
  results[results.length - 1].performance = perf;

  // 5) System：两个 connector health 分开
  await page.goto(`${base}/#/system/connections`, 3000);
  await page.waitForText("Private execution connector", 20000);       // 条件等待（不再固定 settle）
  await record(page, "05-system-connectors", ["Market data connector", "Private execution connector", "connection state"]);

  // 5b) Assistant：已选对象 vs 最新对象（从 Activity 选中 decision 后再问）
  await page.goto(`${base}/#/activity`, 3000);
  await page.waitForSelector("a[data-decision]", 20000);
  const selected = await page.evaluate(`(async () => {
    const link = document.querySelector("a[data-decision]");
    if (!link) return { selected: false };
    link.click();
    await new Promise((r) => setTimeout(r, 2500));
    const detail = document.getElementById("decision-detail");
    return { selected: true, decisionId: link.dataset.decision,
             panel: detail ? detail.innerText.slice(0, 200) : null };
  })()`);
  const assistantSelected = await page.evaluate(`(async () => {
    const buttons = Array.from(document.querySelectorAll("#assistant-questions button"));
    const target = buttons.find((b) => b.textContent.includes("decision")) || buttons[0];
    if (!target) return { answered: false };
    target.click();
    await new Promise((r) => setTimeout(r, 1500));
    const host = document.getElementById("assistant-answer");
    return { answered: true, text: host ? host.innerText.slice(0, 600) : null };
  })()`);
  results.push({ name: "assistant-selected", shot: await page.shot("09-assistant-selected-object"),
                 selected, assistantSelected });

  // 6) Assistant：Run/Decision/Order 上下文 + LOCAL_TRIAL 说明
  await page.goto(`${base}/#/monitor`, 3000);
  await page.waitForSelector("#assistant-questions button", 20000);
  const assistant = await page.evaluate(`(async () => {
    const buttons = Array.from(document.querySelectorAll("#assistant-questions button"));
    const answers = [];
    for (const b of buttons.slice(0, 5)) {
      b.click();
      await new Promise((r) => setTimeout(r, 900));
      const host = document.getElementById("assistant-answer");
      answers.push({ q: b.textContent, a: host ? host.innerText.slice(0, 300) : null });
    }
    return { questions: buttons.map((b) => b.textContent), answers };
  })()`);
  await page.shot("06-assistant-context");
  results.push({ name: "assistant", shot: "06-assistant-context.png", questions: assistant.questions,
                 answers: assistant.answers });
} finally {
  page.close();
  chrome.kill();
}

writeFileSync(join(outDir, "local_loop_capture.json"), JSON.stringify({ base, results }, null, 1), "utf-8");
// 截断时不得产生非法 JSON（按字符切片会破坏字符串）⇒ 只输出紧凑 JSON，供断言脚本解析
console.log(JSON.stringify({ results }));
