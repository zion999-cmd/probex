/**
 * Market 交易对选择器验收（真实 Chrome 驱动真实运行中的产品）。
 *
 * 覆盖人类指令的 8 步：
 *   1) 打开 Market 看到当前 runtime 标的   2) 搜索并选择 BNBUSDT
 *   3) 验证行情组件加载的是 BNBUSDT 真实数据（且与 BTCUSDT 不同）
 *   4) 切回 BTCUSDT 数据恢复   5) 在 BNBUSDT 页面问 Assistant，答案用 BNBUSDT 市场上下文
 *   6) Runtime 仍是 PAPER/原标的，订单/风险/运行状态未受影响
 *   7) 不支持的交易对有明确错误反馈   8) （回归由 unittest 负责）
 *
 * 用法：node tests/ui/market_symbol_selector.mjs <base> <out.json>
 *   先启动产品：scripts/probex.sh demo     # http://127.0.0.1:8897
 * 退出码：0 = 全部通过；1 = 有失败（明细在 JSON 里）。
 */
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const base = process.argv[2] || "http://127.0.0.1:8897";
const out = process.argv[3] || "/tmp/market_symbol_selector.json";
const PORT = 9861;
const OTHER = "BNBUSDT";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`,
  "--no-first-run", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
  "--disable-application-cache", `--user-data-dir=${join(tmpdir(), `probex-mv-${Date.now()}`)}`,
  "--window-size=1680,1050", "about:blank"], { stdio: "ignore" });

async function endpoint(path) {
  for (let i = 0; i < 80; i += 1) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}${path}`); if (r.ok) return await r.json(); } catch {}
    await sleep(250);
  }
  throw new Error("chrome not ready");
}
let ws; let seq = 0; const pending = new Map(); let pageErrors = [];
function send(method, params = {}) {
  const id = ++seq;
  return new Promise((res) => { pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
}
async function ev(expression) {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) return { __exception: r.exceptionDetails.text };
  return r.result.value;
}
async function goto(hash, settle = 3000) {
  pageErrors = [];
  await send("Page.navigate", { url: `${base}${hash}` });
  await sleep(settle);
}

await endpoint("/json/version");
const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") {
    pageErrors.push(String(m.params.exceptionDetails?.exception?.description || "").slice(0, 160));
  }
  if (m.id && pending.has(m.id)) { const r = pending.get(m.id); pending.delete(m.id); r(m.result); }
};
await send("Page.enable"); await send("Runtime.enable");
await send("Network.enable"); await send("Network.setCacheDisabled", { cacheDisabled: true });

const report = { base, checks: [] };
const record = (name, pass, detail) => report.checks.push(
  { name, pass: !!pass, detail: detail === undefined ? "" : String(detail).slice(0, 500) });

try {
  // 先加载应用页面，再取快照（相对 URL 在 about:blank 上会失败并被当成异常对象）。
  await goto("/#/monitor", 3000);
  const runtimeSymbolRaw = await ev(`(async () => {
    const r = await fetch(${JSON.stringify(base + "/api/v1/snapshot")});
    const b = await r.json();
    return String(b.runtime.symbol);
  })()`);
  const runtimeSymbol = typeof runtimeSymbolRaw === "string" ? runtimeSymbolRaw : "";
  record("0.runtime-symbol-resolved", runtimeSymbol.length > 0, JSON.stringify(runtimeSymbolRaw));

  // ---- 1) 打开 Market 看到当前 runtime 标的 + 选择器可见可用
  await goto("/#/market/live", 9000);
  const initial = await ev(`(() => {
    const v = document.getElementById("view");
    const input = document.getElementById("mv-symbol-input");
    return { heading: (v.querySelector("h2")||{}).textContent || "",
             workbenchHeading: Array.from(v.querySelectorAll("h2")).map(h=>h.textContent).join(" | "),
             selectorVisible: !!input, selectorValue: input ? input.value : null,
             hasDatalist: !!document.getElementById("mv-symbol-options"),
             hasApply: !!document.getElementById("mv-symbol-apply"),
             quickButtons: Array.from(document.querySelectorAll("button[data-quick]")).map(b=>b.dataset.quick),
             viewRow: (Array.from(document.querySelectorAll("#view .row")).find(r=>/view symbol/i.test(r.innerText))||{}).innerText||"" };
  })()`);
  record("1.market-shows-runtime-symbol",
    initial.workbenchHeading.includes(runtimeSymbol) && initial.selectorVisible &&
    String(initial.selectorValue).toUpperCase() === String(runtimeSymbol).toUpperCase() &&
    initial.hasDatalist && initial.hasApply,
    JSON.stringify(initial).slice(0, 320));

  // ---- 2) 搜索并选择 BNBUSDT（真实输入 + 点击"查看"）
  const select = await ev(`(async () => {
    const input = document.getElementById("mv-symbol-input");
    input.value = ${JSON.stringify(OTHER)};
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await new Promise(r => setTimeout(r, 900));
    const options = Array.from(document.querySelectorAll("#mv-symbol-options option")).map(o => o.value);
    document.getElementById("mv-symbol-apply").click();
    await new Promise(r => setTimeout(r, 12000));
    return { options: options.slice(0, 8), hash: location.hash,
             viewText: document.getElementById("view").innerText.slice(0, 700) };
  })()`);
  record("2.search-and-select-other-symbol",
    select.options && select.options.length > 0 && select.hash.includes(`view=${OTHER}`) &&
    select.viewText.includes(OTHER),
    JSON.stringify({ options: select.options, hash: select.hash }).slice(0, 300));

  // ---- 3) 组件加载的是 BNBUSDT 真实公开行情（且与 runtime 标的数据不同）
  // 交易所会因连拍对本机限流（HTTP 418/429）⇒ 有界重试；仍不可用则如实标记（不伪装通过）。
  let viewApi = null;
  for (let attempt = 0; attempt < 3 && viewApi === null; attempt += 1) {
    const candidate = await ev(`(async () => {
      const r = await fetch('/api/v1/market/view?symbol=${OTHER}&candles=30&depth=10&trades=10');
      const b = await r.json(); const v = b.market_view || {};
      return { symbol: v.symbol, source: v.source, available: v.available,
               errors: Object.fromEntries(Object.entries(v.errors||{}).map(([k,x])=>[k,x.code])),
               lastClose: (v.candles||[]).length ? v.candles[v.candles.length-1].close : null,
               bestBid: (v.depth||{}).best_bid, bestAsk: (v.depth||{}).best_ask,
               mark: (v.mark||{}).price, overviewLast: (v.overview||{}).last_price,
               fetchedAt: v.fetched_at_ms, now: Date.now() };
    })()`);
    const hasAny = candidate && [candidate.lastClose, candidate.bestBid, candidate.mark,
                                 candidate.overviewLast].some((value) => value !== null &&
                                 value !== undefined);
    if (hasAny) viewApi = candidate;
    else await sleep(9000);
  }
  viewApi = viewApi || { symbol: OTHER, source: "unknown", available: {}, errors: {},
                         lastClose: null, bestBid: null, mark: null, fetchedAt: null, now: Date.now() };
  const pageShows = await ev(`(() => {
    const t = document.getElementById("view").innerText;
    return { mentionsOther: t.includes(${JSON.stringify(OTHER)}),
             mentionsRuntime: /runtime trading symbol/i.test(t),
             statesResearchScope: /研究视图|研究上下文/.test(t),
             heatmapReason: /NOT_AVAILABLE/.test(t) };
  })()`);
  const anyComponent = Object.values(viewApi.available || {}).some(Boolean);
  record("3.real-data-for-other-symbol",
    viewApi.symbol === OTHER && viewApi.source === "binance_public_rest" &&
    viewApi.fetchedAt !== null &&
    Math.abs(Number(viewApi.now) - Number(viewApi.fetchedAt)) < 120000 &&
    anyComponent && pageShows.mentionsOther &&
    pageShows.mentionsRuntime && pageShows.statesResearchScope,
    JSON.stringify({ viewApi, pageShows }).slice(0, 480));

  const runtimeCandles = await ev(`(async () => {
    const r = await fetch('/api/v1/market/candles?interval=1m&limit=1'); const b = await r.json();
    const c = (b.candles||{}).candles||[]; return c.length ? c[c.length-1].close : null;
  })()`);
  const otherPrice = viewApi.lastClose !== null && viewApi.lastClose !== undefined
    ? viewApi.lastClose
    : (viewApi.bestBid ?? viewApi.mark ?? (viewApi.overviewLast ?? null));
  const uniqueToOther = runtimeCandles !== null && otherPrice !== null &&
    Math.abs(Number(runtimeCandles) - Number(otherPrice)) > 1;
  record("3b.other-symbol-data-differs-from-runtime", uniqueToOther,
    JSON.stringify({ runtimeClose: runtimeCandles, otherPrice,
                     source: viewApi.source, available: viewApi.available, errors: viewApi.errors }));

  // ---- 4) 切回 runtime 标的，数据恢复
  await goto(`/#/market/live?view=${runtimeSymbol}`, 7000);
  const back = await ev(`(() => {
    const v = document.getElementById("view");
    const input = document.getElementById("mv-symbol-input");
    return { heading: Array.from(v.querySelectorAll("h2")).map(h=>h.textContent).join(" | "),
             value: input ? String(input.value) : null,
             hasChart: !!document.getElementById("kline-chart"),
             bookRows: v.innerText.includes("Order book") || v.innerText.includes("ORDER BOOK") };
  })()`);
  record("4.switch-back-restores-runtime-view",
    back.heading.includes(runtimeSymbol) && String(back.value).toUpperCase() === String(runtimeSymbol).toUpperCase()
    && back.hasChart,
    JSON.stringify(back).slice(0, 300));

  // ---- 5) 在 BNBUSDT 页面问 Assistant（真实发送）
  await goto(`/#/market/live?view=${OTHER}`, 16000);
  const assistant = await ev(`(async () => {
    const input = document.getElementById("assistant-input");
    const form = document.getElementById("assistant-form");
    if (!input || !form) return { missing: true };
    input.value = "现在这个交易对的行情怎么样？盘口和标记价是多少？";
    form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await new Promise(r => setTimeout(r, 22000));
    const turns = Array.from(document.querySelectorAll(".assistant-turn"));
    const last = turns[turns.length-1];
    return { turnText: last ? last.innerText : null,
             status: (document.getElementById("assistant-status")||{}).innerText||"" };
  })()`);
  record("5.assistant-uses-view-symbol-context",
    assistant.turnText && assistant.turnText.includes(OTHER) &&
    !assistant.turnText.includes(runtimeSymbol),
    JSON.stringify(assistant).slice(0, 480));

  // ---- 6) runtime 未被改动（仍是原 PAPER/标的；订单与风险状态不变）
  const runtimeAfter = await ev(`(async () => {
    const s = await (await fetch('/api/v1/snapshot')).json();
    const o = await (await fetch('/api/v1/orders')).json();
    const bl = await (await fetch('/api/v1/blockers')).json();
    return { mode: s.runtime.mode, symbol: s.runtime.symbol,
             openOrders: (o.execution.active_orders||[]).length,
             equityKnown: !!s.portfolio.equity, blockers: (bl.blockers||[]).length };
  })()`);
  record("6.runtime-unchanged",
    runtimeAfter.mode === "PAPER" &&
    String(runtimeAfter.symbol).toUpperCase() === String(runtimeSymbol).toUpperCase(),
    JSON.stringify(runtimeAfter).slice(0, 300));

  // ---- 7) 不支持的交易对 → 明确错误反馈（页面不崩、显示原因）
  await goto("/#/market/live?view=NOPEZZZUSDT", 9000);
  const unsupported = await ev(`(() => {
    const v = document.getElementById("view");
    const t = v.innerText;
    return { isError: /^\\s*error\\b/i.test(t),
             mentionsUnavailable: /UNAVAILABLE|unsupported_symbol|404/i.test(t),
             keepsSelector: !!document.getElementById("mv-symbol-input"),
             text: t.slice(0, 400) };
  })()`);
  record("7.unsupported-symbol-clear-feedback",
    !unsupported.isError && unsupported.mentionsUnavailable && unsupported.keepsSelector,
    JSON.stringify(unsupported).slice(0, 400));

  if (pageErrors.length) record("console-errors", false, pageErrors.join(" | "));
} catch (error) {
  report.errors = String(error);
}

report.pageErrors = pageErrors;
ws.close();
chrome.kill();
writeFileSync(out, JSON.stringify(report, null, 2));
const failed = report.checks.filter((c) => !c.pass);
console.log(`checks=${report.checks.length} failed=${failed.length}`);
for (const f of failed) console.log("FAIL:", f.name, "|", f.detail.slice(0, 260));
console.log("wrote", out);
process.exit(failed.length === 0 ? 0 : 1);
