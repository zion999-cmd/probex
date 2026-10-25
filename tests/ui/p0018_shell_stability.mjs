/**
 * P0018 Shell/Monitor 稳定性 DOM 验收（真实 Chrome 驱动真实产品）。
 *
 * 验证 SC 1–8：
 * - 10s 动态运行内 0 browser reload、0 whole-workspace DOM replacement；
 * - workspace root / Assistant / 静态卡片 DOM identity 保持；
 * - 只有数据节点变化（MutationObserver 统计 #view 顶层替换 = 0）；
 * - 刷新不重置滚动；Assistant 展开 + 未发送输入在刷新后保留；
 * - 全局条带是聚合摘要（无 .blocker-item 原始行），可导航到 Evidence；
 * - 截图 4 张（SC11，1728×960）写入 <outdir>/。
 *
 * 用法：
 *   node tests/ui/p0018_shell_stability.mjs http://127.0.0.1:<port> <outdir>
 * 退出码：0 全过；1 有失败（明细 JSON 打印 + 写入 <outdir>/p0018_result.json）。
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const base = process.argv[2] || "http://127.0.0.1:8892";
const outdir = process.argv[3] || "/tmp/p0018";
mkdirSync(outdir, { recursive: true });
const PORT = 9761;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`,
  "--no-first-run", "--no-sandbox",
  "--disable-gpu", "--hide-scrollbars", "--disable-application-cache",
  `--user-data-dir=${join(tmpdir(), "probex-p0018-" + Date.now())}`,
  "--window-size=1728,960", "about:blank"], { stdio: "ignore" });

async function endpoint(path) {
  for (let i = 0; i < 80; i += 1) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}${path}`); if (r.ok) return await r.json(); } catch {}
    await sleep(250);
  }
  throw new Error("chrome not ready");
}

let ws; let seq = 0; const pending = new Map();
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
  await send("Page.navigate", { url: `${base}${hash}?t=${Date.now()}` });
  await sleep(settle);
}
async function screenshot(name) {
  const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  if (r.data) {
    writeFileSync(join(outdir, name), Buffer.from(r.data, "base64"));
    return true;
  }
  return false;
}

await endpoint("/json/version");
const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
};
await send("Page.enable"); await send("Runtime.enable");
await send("Network.enable"); await send("Network.setCacheDisabled", { cacheDisabled: true });

const report = { base, checks: [], errors: [] };
const record = (name, pass, detail = "") => {
  report.checks.push({ name, pass: !!pass, detail: String(detail).slice(0, 500) });
  if (!pass) console.error("FAIL:", name, detail);
};

const STATIC_SELECTORS = [
  "#monitor-primary", "#monitor-trends", "#monitor-orders", "#monitor-activity",
  "#monitor-diagnostic", "#monitor-attention", "#monitor-actions",
  '[data-bind="equity"]', '[data-bind="market_health"]', "#spark-equity",
];

try {
  await goto("/#/monitor", 4000);

  // ---- 安装监测：reload、#view 顶层替换、静态节点移除、scroll
  await ev(`(() => {
    window.__p0018 = { reloads: 0, viewChildrenReplaced: 0, removedStatic: [], scrollResets: 0,
      removedNodeNames: [] };
    const origReload = location.reload.bind(location);
    location.reload = () => { window.__p0018.reloads += 1; };
    const view = document.getElementById("view");
    window.__p0018.viewIdentity = view;
    new MutationObserver((muts) => {
      for (const m of muts) {
        for (const node of m.removedNodes) {
          if (node.nodeType !== 1) continue;
          if (node.id === "monitor-primary" || node.classList?.contains("monitor-primary")) {
            window.__p0018.viewChildrenReplaced += 1;
          }
          window.__p0018.removedNodeNames.push(node.id || node.className || node.tagName);
        }
      }
    }).observe(view, { childList: true, subtree: true });
    window.__p0018.static = {};
    ${STATIC_SELECTORS.map((s, i) => `window.__p0018.static[${i}] = document.querySelector(${JSON.stringify(s)});`).join("\n")}
    window.__p0018.assistantIdentity = document.getElementById("assistant-drawer");
    window.__p0018.viewScrollBefore = document.querySelector("main")?.scrollTop || 0;
    return "armed";
  })()`);

  // ---- SC3：滚动后刷新不重置。
  // 新 Monitor 在 1728×960 下内容天然不溢出；注入测试样式让 workspace 容器
  // （#view 本身）成为可滚动区域，验证 targeted patch 不会重建/重置它。
  await ev(`(() => {
    const style = document.createElement("style");
    style.id = "__p0018-scroll-probe";
    style.textContent = "#view{height:420px!important;overflow:auto!important;}";
    document.head.appendChild(style);
    const m = document.getElementById("view");
    window.__p0018.scrollContainer = "#view";
    m.scrollTop = 250;
    return { scrollable: m.scrollHeight > m.clientHeight, top: m.scrollTop };
  })()`);
  // ---- SC4：Assistant 展开 + 输入未发送文本
  await ev(`(() => {
    const drawer = document.getElementById("assistant-drawer");
    drawer.classList.remove("collapsed");
    try { sessionStorage.setItem("probex.assistant.open", "open"); } catch {}
    const input = document.getElementById("assistant-input");
    input.value = "为什么现在没有下单？(draft)";
    return { collapsed: drawer.classList.contains("collapsed"), value: input.value };
  })()`);

  // ---- SC1：10s 动态窗口（surface refresh = 5s，覆盖 ≥1 个完整周期）
  await sleep(10_000);

  const after = await ev(`(() => {
    const w = window.__p0018;
    const staticStable = ${JSON.stringify(STATIC_SELECTORS)}.every((s, i) =>
      document.querySelector(s) === w.static[i]);
    const m = document.querySelector("main");
    const scrollTop = m.scrollTop;
    return {
      reloads: w.reloads,
      viewChildrenReplaced: w.viewChildrenReplaced,
      sameView: document.getElementById("view") === w.viewIdentity,
      sameAssistant: document.getElementById("assistant-drawer") === w.assistantIdentity,
      staticStable,
      scrollTop,
      scrollContainer: w.scrollContainer,
      drawerOpen: !document.getElementById("assistant-drawer").classList.contains("collapsed"),
      draftKept: document.getElementById("assistant-input").value,
      removed: (w.removedNodeNames || []).slice(0, 20),
    };
  })()`);
  record("SC1 zero browser reload", after.reloads === 0, JSON.stringify(after.reloads));
  record("SC1 zero whole-workspace replacement", after.viewChildrenReplaced === 0,
    `replaced=${after.viewChildrenReplaced} removed=${JSON.stringify(after.removed)}`);
  record("SC2 workspace root identity stable", after.sameView);
  record("SC2 assistant identity stable", after.sameAssistant);
  record("SC2 static card/container identity stable", after.staticStable);
  record("SC3 scroll position retained across refresh", after.scrollTop >= 240,
    `scrollTop=${after.scrollTop} (${after.scrollContainer})`);
  record("SC4 assistant stays open across refresh", after.drawerOpen === true);
  record("SC4 unsent assistant input retained", after.draftKept.includes("(draft)"),
    JSON.stringify(after.draftKept));

  // ---- SC5：全局条带聚合，无原始 blocker 行
  const strip = await ev(`(() => {
    const s = document.getElementById("global-status");
    return { text: s ? s.textContent : null, state: s ? s.dataset.state : null,
             rawRows: document.querySelectorAll("#strip .blocker-item").length,
             href: s ? s.getAttribute("href") : null };
  })()`);
  record("SC5 strip is aggregated summary (no raw rows)", strip.rawRows === 0 &&
    /blockers?|ALL CLEAR/i.test(strip.text || ""), JSON.stringify(strip));
  record("SC6 blocker summary navigates to evidence", /activity\/evidence/.test(strip.href || "") &&
    /focus=blockers/.test(strip.href || ""), strip.href);

  // 移除 SC3 滚动探针样式，恢复第一屏真实布局
  await ev(`(() => { document.getElementById("__p0018-scroll-probe")?.remove();
    document.getElementById("view").scrollTop = 0; window.scrollTo(0, 0); return 1; })()`);
  await sleep(300);

  // ---- SC7：第一屏无需滚动即可判断（primary band 字段齐备）
  const firstScreen = await ev(`(() => {
    const keys = ["mode","instrument","runtime_state","overall_health","equity","realized_pnl",
      "unrealized_pnl","position","exposure","market_health","strategy_state","execution_health",
      "readiness","attention"];
    const present = keys.filter((k) => document.querySelector('[data-bind="' + k + '"]'));
    const primary = document.getElementById("monitor-primary");
    const rect = primary.getBoundingClientRect();
    return { allPresent: present.length === keys.length, missing: keys.filter((k) => !present.includes(k)),
             primaryTopWithinViewport: rect.top >= 0, primaryBottomVisible: rect.bottom <= innerHeight };
  })()`);
  record("SC7 primary band carries mode/runtime/health/equity/pnl/position/exposure/market/strategy/execution/blockers",
    firstScreen.allPresent, JSON.stringify(firstScreen.missing));
  record("SC7 primary band visible without scroll",
    firstScreen.primaryTopWithinViewport && firstScreen.primaryBottomVisible,
    JSON.stringify({ top: firstScreen.primaryTopWithinViewport, bottom: firstScreen.primaryBottomVisible }));

  // ---- SC8：UNKNOWN 语义保留（存在 unknown 类或明确 UNKNOWN 文本，但无伪装 healthy 空值）
  const unknownSemantics = await ev(`(() => {
    const binds = [...document.querySelectorAll("[data-bind]")];
    const texts = binds.map((b) => b.textContent.trim());
    const hasUnknown = texts.some((t) => t === "UNKNOWN");
    const noEmptyValues = binds.every((b) => b.textContent.trim() !== "");
    return { hasUnknown, noEmptyValues, sample: texts.slice(0, 14) };
  })()`);
  record("SC8 UNKNOWN semantics preserved (no empty placeholder values)",
    unknownSemantics.noEmptyValues, JSON.stringify(unknownSemantics.sample));

  // ---- 原始动作清单不再平铺（最多 1 个运行时按钮：reconcile）
  const actionNoise = await ev(`document.querySelectorAll("#monitor-actions button").length`);
  record("Monitor actions reduced (<=1 runtime button)", actionNoise <= 1, `buttons=${actionNoise}`);

  // ---- SC11 截图：collapsed / expanded / scroll / blocker drill-down
  await ev(`(() => { document.getElementById("assistant-drawer").classList.add("collapsed");
    document.querySelector("main").scrollTop = 0; return 1; })()`);
  await sleep(400);
  const shots = [];
  shots.push(await screenshot("p0018-monitor-collapsed.png"));
  await ev(`document.getElementById("assistant-drawer").classList.remove("collapsed")`);
  await sleep(400);
  shots.push(await screenshot("p0018-monitor-assistant-expanded.png"));
  // 滚动截图：临时收缩 viewport 高度制造真实滚动（SC11 要求 lower/scroll state）
  await send("Emulation.setDeviceMetricsOverride",
    { width: 1728, height: 620, deviceScaleFactor: 1, mobile: false });
  await sleep(300);
  await ev(`document.querySelector("main").scrollTop = 200`);
  await sleep(300);
  shots.push(await screenshot("p0018-monitor-scrolled.png"));
  await send("Emulation.clearDeviceMetricsOverride", {});
  await goto("/#/activity/evidence?focus=blockers", 3000);
  shots.push(await screenshot("p0018-blocker-drilldown.png"));
  record("SC11 four screenshots captured", shots.every(Boolean), shots.join(","));
} catch (error) {
  report.errors.push(String(error && error.stack || error).slice(0, 800));
} finally {
  writeFileSync(join(outdir, "p0018_result.json"), JSON.stringify(report, null, 2));
  chrome.kill();
}

const failed = report.checks.filter((c) => !c.pass);
console.log(`\nP0018: ${report.checks.length - failed.length}/${report.checks.length} passed`);
process.exit(failed.length || report.errors.length ? 1 : 0);
