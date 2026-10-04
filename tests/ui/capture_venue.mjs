/**
 * P0001.15 acceptance F：真实浏览器截图（Chrome headless + CDP）。
 *
 * 六个表面都必须显示 instrument / venue / reference price / connector 事实：
 *   1. Monitor（instrument / asset class / product type / venue / reference price source）
 *   2. Market（market connector + data source + MARK 状态）
 *   3. Activity（instrument_id + venue_id + Decision→Order 因果链）
 *   4. Order detail（decision_id / client_order_id / venue_order_id / connector）
 *   5. System（MarketDataConnector 与 PrivateExecutionConnector **分开**显示 health）
 *   6. Assistant（Spot vs Perpetual / MARK 来源 / fail-closed 原因 / 订单来自哪个 decision / 哪个 connector）
 *
 * 每个表面同时返回「真实可见文本断言」，避免只截图不自证。
 * 用法：node tests/ui/capture_venue.mjs <base-url> <out-dir>
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [base, outDir] = [process.argv[2], process.argv[3] || "/tmp/probex_venue/shots"];
const PORT = 9600 + Math.floor(Math.random() * 300);
mkdirSync(outDir, { recursive: true });

const chrome = spawn(CHROME, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${join(tmpdir(), `probex-venue-${PORT}`)}`,
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
    const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`,
      { method: "PUT" })).json();
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
    const page = new Page(ws);
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && page.pending.has(message.id)) {
        const { resolve, reject } = page.pending.get(message.id);
        page.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error))); else resolve(message.result);
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
  async goto(url, settleMs = 2500) {
    await this.send("Page.navigate", { url });
    await sleep(settleMs);
  }
  async text() {
    const result = await this.send("Runtime.evaluate", {
      expression: "document.body.innerText", returnByValue: true,
    });
    return String(result.result.value || "");
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    return result.result.value;
  }
  async shot(name) {
    const result = await this.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    writeFileSync(join(outDir, `${name}.png`), Buffer.from(result.data, "base64"));
    return `${name}.png`;
  }
  close() { this.ws.close(); }
}

const EXPECTED = {
  monitor: ["instrument", "asset class", "product type", "venue", "reference price", "reference source"],
  market: ["Instrument / market data source", "market connector", "mark / reference price"],
  activity: ["instrument", "venue", "decision → orders"],
  orders: ["decision", "instrument", "venue", "execution connector"],
  connections: ["Market data connector", "Private execution connector", "connection state"],
  instruments: ["Instrument spec", "Capabilities (product semantics)", "Reference price policy"],
};

async function surface(page, name, hash, { expect = [] } = {}) {
  await page.goto(`${base}/${hash}`);
  const text = await page.text();
  const missing = expect.filter((needle) => !text.toLowerCase().includes(needle.toLowerCase()));
  const shot = await page.shot(name);
  return { name, shot, missing, excerpt: text.slice(0, 400).replace(/\s+/g, " ") };
}

await endpoint("/json/version");        // 等 Chrome CDP 真正就绪（否则 /json/new 会 ECONNREFUSED）
const results = [];
const page = await Page.open("about:blank");
try {
  results.push(await surface(page, "01-monitor-instrument-venue", "#/monitor", { expect: EXPECTED.monitor }));
  results.push(await surface(page, "02-market-instrument-source", "#/market/live", { expect: EXPECTED.market }));
  results.push(await surface(page, "03-activity-causal-chain", "#/activity", { expect: EXPECTED.activity }));
  results.push(await surface(page, "04-order-detail", "#/activity/orders", { expect: EXPECTED.orders }));
  results.push(await surface(page, "05-system-connector-health", "#/system/connections",
    { expect: EXPECTED.connections }));
  results.push(await surface(page, "06-system-instrument-spec", "#/system/instruments",
    { expect: EXPECTED.instruments }));

  // Assistant：点击 P0001.15 的五个问题（确定性 explain，不调用 LLM），截图答案
  await page.goto(`${base}/#/monitor`);
  const questions = await page.evaluate(
    "Array.from(document.querySelectorAll('#assistant-questions button')).map((b) => b.textContent)");
  const answers = [];
  for (let index = 0; index < (questions || []).length; index += 1) {
    await page.evaluate(`document.querySelectorAll('#assistant-questions button')[${index}].click()`);
    await sleep(600);
    answers.push({
      question: questions[index],
      answer: await page.evaluate("document.getElementById('assistant-answer') ? " +
        "document.getElementById('assistant-answer').innerText : null"),
    });
  }
  const shot = await page.shot("07-assistant-instrument-venue");
  results.push({ name: "assistant", shot, questions: questions || [], answers: answers.slice(0, 5) });
} finally {
  page.close();
  chrome.kill();
}

writeFileSync(join(outDir, "venue_capture.json"), JSON.stringify({ base, results }, null, 1), "utf-8");
console.log(JSON.stringify({ shots: results }, null, 1));
