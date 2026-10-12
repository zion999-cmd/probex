/**
 * Market View 交易对选择器（人类指令：Market View Symbol ≠ Runtime Trading Symbol）。
 *
 * - **Market View Symbol**：用户当前查看/研究的交易对，可在此切换（纯研究上下文）。
 * - **Runtime Trading Symbol**：运行实例实际交易的标的，由启动参数固定；本选择器**不会**修改它，
 *   不重启运行实例，不触发任何交易动作。
 *
 * 选中即写入 hash：`#/market/live?view=<SYMBOL>`（可分享、可刷新保持），由 Market 页面加载真实行情。
 */
import { ENDPOINTS, fetchJson } from "/ui/client/api.js";
import { escapeHtml, fact } from "/ui/client/render.js";

/** 常用快速选择（只是快捷入口；真实受支持性由交易所目录裁决，不在此伪造）。 */
export const QUICK_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"];

export function normalizeSymbol(value) {
  return String(value || "").trim().toUpperCase();
}

export function marketViewHash(symbol) {
  return `#/market/live?view=${encodeURIComponent(normalizeSymbol(symbol))}`;
}

/** 选择器 HTML（原生 datalist：可输入可搜索，键盘可操作）。 */
export function symbolSelectorHtml({ viewSymbol, runtimeSymbol, sameAsRuntime }) {
  const quick = QUICK_SYMBOLS.map((symbol) =>
    `<button type="button" data-quick="${escapeHtml(symbol)}" ` +
    `class="${symbol === viewSymbol ? "active" : ""}">${escapeHtml(symbol)}</button>`).join("");
  return `
    <div class="toolbar symbol-selector">
      <span class="wb-label">market view symbol</span>
      <input id="mv-symbol-input" list="mv-symbol-options" value="${escapeHtml(viewSymbol)}"
             placeholder="搜索交易对，例如 BNBUSDT" aria-label="选择要查看的交易对"
             autocomplete="off" />
      <datalist id="mv-symbol-options"></datalist>
      <button type="button" id="mv-symbol-apply">查看</button>
      ${quick}
    </div>
    <div class="row"><span class="k">view symbol</span><span class="v">${escapeHtml(viewSymbol)}
      ${sameAsRuntime ? '<span class="known">= runtime trading symbol</span>'
        : '<span class="unknown">≠ runtime trading symbol</span>'}</span></div>
    <div class="row"><span class="k">runtime trading symbol</span><span class="v">${escapeHtml(runtimeSymbol)}
      <span class="muted">（本运行实例的标的，固定于启动参数；查看其它交易对不会修改它）</span></span></div>
    <div class="row"><span class="k">symbol catalog</span><span class="v muted" id="mv-catalog">…</span></div>`;
}

/** 绑定选择器交互：搜索（datalist）、应用、快捷选择。 */
export function mountSymbolSelector() {
  const input = document.getElementById("mv-symbol-input");
  const apply = document.getElementById("mv-symbol-apply");
  const options = document.getElementById("mv-symbol-options");
  const catalog = document.getElementById("mv-catalog");
  if (!input || !apply) return;
  const navigate = (symbol) => {
    const target = normalizeSymbol(symbol);
    if (!target) return;
    window.location.hash = marketViewHash(target).slice(1);
  };
  apply.addEventListener("click", () => navigate(input.value));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); navigate(input.value); }
  });
  document.querySelectorAll("button[data-quick]").forEach((button) => {
    button.addEventListener("click", () => navigate(button.dataset.quick));
  });
  // 搜索建议：来自交易所目录（真实受支持集合）；失败则显式 UNKNOWN，不编造候选。
  let timer = null;
  const load = async (query) => {
    try {
      const payload = await fetchJson(
        `${ENDPOINTS.marketViewSymbols}?q=${encodeURIComponent(query || "")}&limit=50`);
      const view = payload.market_view || {};
      if (options) {
        options.innerHTML = (view.symbols || []).map((row) =>
          `<option value="${escapeHtml(row.symbol)}">${escapeHtml(row.contract_type || "")}</option>`).join("");
      }
      if (catalog) {
        catalog.innerHTML = `${view.total ?? "UNKNOWN"} 个可查看交易对（source=${escapeHtml(view.source || "unknown")}）`;
      }
    } catch (error) {
      if (catalog) {
        catalog.innerHTML = `<span class="unknown">UNKNOWN (${escapeHtml(String(error))})</span> ` +
          `<span class="muted">目录不可用时仍可直接输入交易对并查看</span>`;
      }
    }
  };
  input.addEventListener("input", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => load(input.value), 250);
  });
  void load("");
}

/** 公开行情视图的键值区块（每项都显示真实可用性与原因，不用 0 冒充缺失）。 */
export function marketViewFacts(view) {
  const available = view.available || {};
  const errors = view.errors || {};
  const reason = (name) => {
    const entry = errors[name];
    if (!entry) return "";
    return ` <span class="unknown">${escapeHtml(entry.code)}</span>`;
  };
  const overview = view.overview || {};
  const depth = view.depth || {};
  const mark = view.mark || {};
  const rows = [
    ["source", `${escapeHtml(view.source || "unknown")} · venue=${escapeHtml(view.venue || "")}`],
    ["fetched at", fact(view.fetched_at_ms)],
    ["24h last / change", available.overview
      ? `${escapeHtml(String(overview.last_price))} (${escapeHtml(String(overview.change_pct_24h))}%)`
      : `<span class="unknown">UNKNOWN</span>${reason("overview")}`],
    ["24h high / low", available.overview
      ? `${escapeHtml(String(overview.high_24h))} / ${escapeHtml(String(overview.low_24h))}`
      : `<span class="unknown">UNKNOWN</span>${reason("overview")}`],
    ["bid / ask", available.depth
      ? `${escapeHtml(String(depth.best_bid))} / ${escapeHtml(String(depth.best_ask))}`
      : `<span class="unknown">UNKNOWN</span>${reason("depth")}`],
    ["spread / mid", available.depth
      ? `${escapeHtml(String(depth.spread))} / ${escapeHtml(String(depth.mid))}`
      : `<span class="unknown">UNKNOWN</span>${reason("depth")}`],
    ["mark price", available.mark
      ? `${escapeHtml(String(mark.price))} <span class="muted">(index=${escapeHtml(String(mark.index_price))}, funding=${escapeHtml(String(mark.funding_rate))})</span>`
      : `<span class="unknown">UNKNOWN</span>${reason("mark")}`],
    ["candles", available.candles
      ? `${(view.candles || []).length} 根 · interval=${escapeHtml(view.interval || "")}`
      : `<span class="unknown">UNKNOWN</span>${reason("candles")}`],
    ["recent trades", available.trades
      ? `${(view.trades || []).length} 笔`
      : `<span class="unknown">UNAVAILABLE</span>${reason("trades")}`],
    ["notes", escapeHtml((view.notes || []).join("; ") || "none")],
  ];
  return rows;
}

export const L2_HEATMAP_REASON =
  "REST 快照视图只有当前盘口一层，没有连续 L2 序列 ⇒ 不做热力图（运行实例视图才有基于真实 " +
  "L2 事件序列的热力图）";
