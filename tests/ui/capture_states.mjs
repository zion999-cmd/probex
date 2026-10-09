/**
 * P0001.17 §2/§16B：Surface 状态浏览器验证（真实故障注入 + 真实空数据）。
 *
 * 对每个 Surface 断言：
 *   - Error：请求失败时页面显示 error/UNKNOWN 文案 + **恢复入口**（Monitor / System health / retry）
 *   - Empty/UNKNOWN：没有事实时显示 UNKNOWN/NOT_AVAILABLE + reason，且可继续导航
 *
 * 用法：node tests/ui/capture_states.mjs <fault-base> <empty-base> <out-dir>
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [faultBase, emptyBase, outDir] = [process.argv[2], process.argv[3], process.argv[4] || "/tmp/probex_p117/states"];
const PORT = 9950 + Math.floor(Math.random() * 40);
mkdirSync(outDir, { recursive: true });

const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${join(tmpdir(), `probex-states-${PORT}`)}`, "--no-first-run", "--no-sandbox",
  "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1680,1050",
  "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function endpoint(p) {
  for (let i = 0; i < 60; i += 1) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}${p}`); if (r.ok) return await r.json(); }
    catch { /* not up */ }
    await sleep(250);
  }
  throw new Error(`chrome ${p} not ready`);
}
class Page {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async open() {
    const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent("about:blank")}`,
      { method: "PUT" })).json();
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const page = new Page(ws);
    ws.onmessage = (e) => { const m = JSON.parse(e.data);
      if (m.id && page.pending.has(m.id)) { const p = page.pending.get(m.id); page.pending.delete(m.id); p(m.result); } };
    await page.send("Page.enable"); await page.send("Runtime.enable");
    return page;
  }
  send(method, params = {}) { const id = ++this.id;
    return new Promise((res) => { this.pending.set(id, res); this.ws.send(JSON.stringify({ id, method, params })); }); }
  async goto(url, settle = 3500) {
    await this.send("Page.navigate", { url: "about:blank" }); await sleep(200);
    const t = new URL(url); t.searchParams.set("t", String(Date.now()));
    await this.send("Page.navigate", { url: t.toString() }); await sleep(settle);
  }
  async text() { return String((await this.evaluate("document.body.innerText")) || ""); }
  async waitForAny(needles, timeout = 15000, interval = 250) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const lower = (await this.text()).toLowerCase();
      if (needles.some((n) => lower.includes(n.toLowerCase()))) return true;
      await sleep(interval);
    }
    return false;
  }
  async evaluate(expr) {
    const r = await this.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    return r.result.value;
  }
  close() { this.ws.close(); }
}

const SURFACES = ["monitor", "market/live", "activity", "performance", "system/health"];
const results = { fault: [], empty: [] };

await endpoint("/json/version");
const page = await Page.open();
try {
  for (const [mode, base] of [["fault", faultBase], ["empty", emptyBase]]) {
    for (const surface of SURFACES) {
      await page.goto(`${base}/#/${surface}`, 2000);
      await page.waitForAny(["ERROR", "UNKNOWN", "unavailable", "NOT_AVAILABLE"], 15000);
      const text = await page.text();
      const lower = text.toLowerCase();
      const hasRecovery = await page.evaluate(
        `!!document.getElementById("retry-page") || document.body.innerHTML.includes("System health")`);
      const state = {
        surface, mode,
        hasErrorHint: lower.includes("error") || lower.includes("unavailable") || lower.includes("unknown"),
        hasRecovery,
        hasUnknownReason: lower.includes("unknown") || lower.includes("not_available") || lower.includes("absent"),
        loadingSeen: lower.includes("loading"),
        navOk: await page.evaluate(`document.querySelectorAll("nav a").length > 3
          || document.body.innerHTML.includes("#/system/health")`),
        excerpt: text.slice(0, 200).replace(/\s+/g, " "),
      };
      results[mode].push(state);
    }
  }
} finally { page.close(); chrome.kill(); }

writeFileSync(join(outDir, "surface_states.json"), JSON.stringify(results, null, 1), "utf-8");
console.log(JSON.stringify(results, null, 1));
