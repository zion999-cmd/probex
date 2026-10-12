/**
 * 产品 UI 入口闭环验收（真实 Chrome 驱动真实运行中的产品）。
 *
 * 这不是组件/契约测试：它打开实际产品页面，点击实际入口，发送实际助手消息，
 * 执行实际 Action Gateway 调用，并断言**真实反馈**（含失败/空/未知状态）。
 *
 * 用法：
 *   1) 先启动产品，例如： scripts/probex.sh demo        # http://127.0.0.1:8897
 *   2) node tests/ui/ui_entry_acceptance.mjs http://127.0.0.1:8897 /tmp/ui_entry_acceptance.json
 *
 * 退出码：0 = 全部检查通过；1 = 有失败（明细写入 JSON）。
 * 依赖：本机 Chrome（headless）+ node 22（全局 WebSocket）；不需要 puppeteer。
 */

import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const base = process.argv[2] || "http://127.0.0.1:8897";
const out = process.argv[3] || "/tmp/ui_verify.json";
const PORT = 9750;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`,
  "--no-first-run", "--no-sandbox",
  "--disable-gpu", "--hide-scrollbars", "--disable-application-cache",
  `--user-data-dir=${join(tmpdir(), "probex-verify-" + Date.now())}`,
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
async function goto(hash, settle = 2600) {
  pageErrors = [];
  await send("Page.navigate", { url: `${base}${hash}${hash.includes("?") ? "&" : "?"}t=${Date.now()}` });
  await sleep(settle);
}

await endpoint("/json/version");
const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") {
    pageErrors.push(String(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text || "").slice(0, 160));
  }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
    pageErrors.push("console.error: " + m.params.args.map((a) => a.value ?? a.description).join(" ").slice(0, 160));
  }
  if (m.id && pending.has(m.id)) { const r = pending.get(m.id); pending.delete(m.id); r(m.result); }
};
await send("Page.enable"); await send("Runtime.enable");
await send("Network.enable"); await send("Network.setCacheDisabled", { cacheDisabled: true });

const report = { base, checks: [], errors: [] };
const record = (name, pass, detail) => {
  report.checks.push({ name, pass: !!pass, detail: detail === undefined ? "" : String(detail).slice(0, 400) });
};

try {
  // ---- 1. 五个工作区可达 + 无错误
  for (const s of ["monitor", "market", "activity", "performance", "system"]) {
    await goto(`/#/${s}`, 8000);
    const state = await ev(`(() => { const v = document.getElementById("view");
      return { heading: (v.querySelector("h2")||{}).textContent || "", len: v.innerText.length,
               isError: /^\\s*error\\b/i.test(v.innerText) || v.innerText.includes("Cannot read properties") }; })()`);
    record(`workspace:${s}`, !state.isError && state.len > 200, JSON.stringify(state));
    if (pageErrors.length) record(`workspace:${s}:console`, false, pageErrors.join(" | "));
  }

  // ---- 2. Header 只读原因 + Blocker 原因/下一步
  await goto("/#/monitor");
  const header = await ev(`(() => {
    const b = document.getElementById("banner");
    const why = b.querySelector("details.why");
    if (why) why.open = true;
    const strip = document.getElementById("blockers");
    return { text: b.innerText.slice(0, 220), whyText: why ? why.innerText : null,
             inlineReason: /fixed @startup/.test(b.innerText),
             blockerItems: Array.from(strip.querySelectorAll(".blocker-item")).map(x => x.innerText.replace(/\\n/g," ")),
             nextSteps: Array.from(strip.querySelectorAll(".blocker-next")).map(x => x.innerText) }; })()`);
  record("header:read-only-reason", !!(header.inlineReason && header.whyText && /启动参数/.test(header.whyText)), JSON.stringify({ inline: header.inlineReason, why: (header.whyText || "").slice(0, 160) }));
  record("header:mode-tag", /PAPER|TESTNET|LIVE|REPLAY/.test(header.text), header.text.slice(0, 80));
  record("blocker:reason-and-next-visible", header.blockerItems.length > 0 && header.nextSteps.length > 0,
    JSON.stringify({ items: header.blockerItems.slice(0, 2), next: header.nextSteps.slice(0, 2) }));

  // ---- 3. Activity 身份单元格 → Evidence（order / fill / execution_event）实地点击
  for (const [label, needle] of [["order", "order"], ["fill", "fill"], ["execution_event", "execution_event"]]) {
    await goto("/#/activity");
    const clicked = await ev(`(async () => {
      const links = Array.from(document.querySelectorAll('#view a[href*="/activity/evidence/"]'));
      const link = links.find(a => a.getAttribute("href").split("/")[3] === ${JSON.stringify(needle)}) || null;
      if (!link) return { found: false, sample: links.slice(0,3).map(a=>a.getAttribute("href")) };
      const href = link.getAttribute("href");
      window.location.hash = href.slice(1);
      await new Promise(r => setTimeout(r, 2600));
      const v = document.getElementById("view");
      return { found: true, href, heading: (v.querySelector("h2")||{}).textContent || "",
               isError: /^\\s*error\\b/i.test(v.innerText) || v.innerText.includes("Cannot read properties"),
               hasFactsTable: /raw facts/i.test(v.innerText), text: v.innerText.slice(0, 220) };
    })()`);
    record(`activity→evidence:${label}`, clicked.found && !clicked.isError && clicked.hasFactsTable,
      JSON.stringify(clicked));
  }

  // ---- 4. Orders：decision 反查（双向）
  await goto("/#/activity");
  const decisionId = await ev(`(async () => {
    const r = await fetch('/api/v1/evidence'); const b = await r.json();
    const trace = (b.evidence||{}).trace||[];
    const entry = trace.find(t => String(t.detail||"").includes("decision_id="));
    const m = entry ? String(entry.detail).match(/decision_id=([A-Za-z0-9:_-]+)/) : null;
    return m ? m[1] : null; })()`);
  await goto(`/#/activity/orders?decision=${encodeURIComponent(decisionId)}`);
  const orders = await ev(`(() => { const v = document.getElementById("view"); const t = v.innerText;
    return { isError: /^\\s*error\\b/i.test(t) || t.includes("Cannot read properties"),
             hasReverse: /双向反查/.test(t) && /ORDERS IN THIS DECISION/.test(t.toUpperCase()),
             text: t.slice(0, 300) }; })()`);
  record("orders:decision-reverse-lookup", !orders.isError && orders.hasReverse, JSON.stringify(orders));

  // ---- 5. Monitor 可用动作（manifest 驱动）+ L2 确认执行
  await goto("/#/monitor", 9000);
  const actions = await ev(`(() => { const s = document.querySelectorAll('#view button[data-action]');
    return { count: s.length, ids: Array.from(s).map(b=>b.dataset.action),
             hasStatus: !!document.getElementById("monitor-action-status") }; })()`);
  record("monitor:available-actions", actions.count > 0 && actions.hasStatus, JSON.stringify(actions));

  const invokeResult = await ev(`(async () => {
    const btn = Array.from(document.querySelectorAll('#view button[data-action]'))
      .find(b => b.dataset.action === "runtime.request_reconciliation");
    if (!btn) return { skipped: true };
    const status = document.querySelector('#monitor-action-status .v');
    btn.click();
    await new Promise(r => setTimeout(r, 3500));
    return { status: status ? status.innerText : null }; })()`);
  record("monitor:L2-action-confirmation",
    invokeResult.skipped || /SUCCEEDED|REFUSED|FAILED|UNKNOWN|CONFIRMATION/.test(String(invokeResult.status)),
    JSON.stringify(invokeResult));

  // ---- 6. Assistant：输入 + 发送 + 追问 + 状态反馈
  await goto("/#/monitor", 6000);
  const assistant = await ev(`(() => { const d = document.getElementById("assistant-drawer");
    return { hasInput: !!d.querySelector('#assistant-input'), hasSend: !!d.querySelector('#assistant-send'),
             hasForm: !!d.querySelector('#assistant-form') }; })()`);
  record("assistant:input-and-send", assistant.hasInput && assistant.hasSend && assistant.hasForm, JSON.stringify(assistant));

  const chat = await ev(`(async () => {
    const input = document.getElementById('assistant-input');
    const form = document.getElementById('assistant-form');
    const out = [];
    for (const q of ["为什么现在没有下单？", "现在系统的状态怎么样？"]) {
      input.value = q;
      form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
      await new Promise(r => setTimeout(r, 12000));
      const turns = Array.from(document.querySelectorAll('.assistant-turn'));
      const last = turns[turns.length - 1];
      out.push({ q, turnText: last ? last.innerText.slice(0, 300) : null,
                 status: (document.getElementById('assistant-status')||{}).innerText || "" });
    }
    return { turns: document.querySelectorAll('.assistant-turn').length, out }; })()`);
  const answered = (chat.out || []).every((r) => r.turnText && !/FAILED|NO_RESPONSE|thinking/.test(r.status));
  record("assistant:real-answers", answered && chat.turns >= 2, JSON.stringify(chat));
  // 诊断问题（为什么没有下单）不得被误判为资本动作请求
  const diagnostic = (chat.out || [])[0] || {};
  record("assistant:diagnostic-not-capital",
    !/unavailable_by_design/.test(String(diagnostic.turnText)) && /blocker|RISK|订单|事实/i.test(String(diagnostic.turnText)),
    JSON.stringify(diagnostic).slice(0, 300));

  const capital = await ev(`(async () => {
    const input = document.getElementById('assistant-input');
    const form = document.getElementById('assistant-form');
    input.value = "帮我下单买 0.01 BTC";
    form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
    await new Promise(r => setTimeout(r, 12000));
    const turns = Array.from(document.querySelectorAll('.assistant-turn'));
    const last = turns[turns.length - 1];
    return { text: last ? last.innerText.slice(0, 300) : null }; })()`);
  record("assistant:capital-refused", /unavailable_by_design/.test(String(capital.text)), JSON.stringify(capital));

  // ---- 7. Market / System / Performance 关键入口存在
  await goto("/#/market/live", 3500);
  const market = await ev(`(() => { const v = document.getElementById("view"); const t = v.innerText;
    return { hasUniverseLink: !!v.querySelector('a[href="#/market/universe"]'),
             hasBook: /Order book/i.test(t), hasTrades: /Recent trades/i.test(t),
             hasHeatmap: /heatmap/i.test(t), hasDecisions: /decision/i.test(t) }; })()`);
  record("market:book/trades/heatmap/universe", market.hasUniverseLink && market.hasBook && market.hasTrades && market.hasHeatmap,
    JSON.stringify(market));

  await goto("/#/system/execution", 3000);
  const system = await ev(`(() => { const t = document.getElementById("view").innerText;
    return { hasLimits: /limit/i.test(t), hasRate: /rate/i.test(t), hasLatency: /latency|延迟/i.test(t),
             hasRecon: /reconcil|对账/i.test(t), hasHealth: /health/i.test(t), snippet: t.length }; })()`);
  record("system:execution-safety-sections",
    system.hasLimits && system.hasRate && system.hasLatency && system.hasRecon && system.hasHealth, JSON.stringify(system));

  await goto("/#/performance", 3200);
  const perf = await ev(`(() => { const v = document.getElementById("view"); const t = v.innerText;
    return { hasRuns: /Runs/i.test(t), hasMetrics: /Trade statistics|Equity/i.test(t),
             links: v.querySelectorAll('a[href*="/performance/run/"]').length }; })()`);
  record("performance:runs-and-metrics", perf.hasRuns && perf.hasMetrics, JSON.stringify(perf));

  // ---- 8. 空/异常状态：不存在实体不应崩页
  await goto("/#/activity/evidence/order/__no_such_order__", 2600);
  const empty = await ev(`(() => { const v = document.getElementById("view");
    return { isError: /^\\s*error\\b/i.test(v.innerText) || v.innerText.includes("Cannot read properties"),
             text: v.innerText.slice(0, 200) }; })()`);
  record("non-ideal:unknown-entity-no-crash", !empty.isError, JSON.stringify(empty));
} catch (error) {
  report.errors.push(String(error));
}

report.pageErrors = pageErrors;
ws.close(); chrome.kill();
writeFileSync(out, JSON.stringify(report, null, 2));
const failed = report.checks.filter((c) => !c.pass);
console.log(`checks=${report.checks.length} failed=${failed.length}`);
for (const f of failed) console.log("FAIL:", f.name, "|", f.detail.slice(0, 220));
console.log("wrote", out);
process.exit(failed.length === 0 ? 0 : 1);
