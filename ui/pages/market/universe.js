/**
 * P0017.1：全标的 Universe（发现 + 订阅状态 + 共享数据集）。
 *
 * 只读：数据全部来自 /api/v1/market/universe|subscriptions|dataset；不直连连接器。
 * 未接线多标的平台时端点返回 503，页面如实展示不可用，不伪造交易对。
 */
import { fetchJson, fetchOrUnavailable } from "/ui/client/api.js";
import { escapeHtml, fact, rows, section, table } from "/ui/client/render.js";

const BASE = "/api/v1/market";
const UNIVERSE_PATH = `${BASE}/universe`;
const SUBSCRIPTIONS_PATH = `${BASE}/subscriptions`;
const DATASET_PATH = `${BASE}/dataset`;

function statusBadge(status) {
  const cls = status === "HEALTHY" ? "known" : (status === "STALE" ? "unknown" : "muted");
  return `<span class="${cls}">${escapeHtml(status)}</span>`;
}

export async function renderUniverse() {
  const [universe, subs] = await Promise.all([
    fetchOrUnavailable(UNIVERSE_PATH),
    fetchOrUnavailable(SUBSCRIPTIONS_PATH),
  ]);
  if (universe.unavailable) {
    return section("Universe", rows([
      ["multi-symbol platform",
       `<span class="unknown">UNAVAILABLE (${escapeHtml(universe.unavailable)})</span>`],
      ["meaning", "当前运行未启用多标的市场数据平台；不伪造交易对列表"],
    ]));
  }
  const symbols = universe.symbols || [];
  const subMap = {};
  if (!subs.unavailable) {
    for (const s of subs.symbols || []) subMap[s.symbol] = s;
  }
  const rows_ = symbols.map((item) => {
    const sub = subMap[item.symbol];
    return [
      `<a href="#/market/universe/${encodeURIComponent(item.symbol)}">${escapeHtml(item.symbol)}</a>`,
      escapeHtml(item.product_type),
      escapeHtml(item.quote_asset),
      escapeHtml(item.status),
      sub ? statusBadge(sub.status) : '<span class="muted">not subscribed</span>',
      sub && sub.last_event_exchange_ts != null
        ? escapeHtml(String(sub.last_event_exchange_ts)) : "—",
      sub ? String(sub.message_count) : "—",
    ];
  });
  return section(
    "Universe (discovered from exchangeInfo)",
    rows([
      ["venue", escapeHtml(universe.venue || "venue")],
      ["quote assets", escapeHtml((universe.quote_assets || []).join(", "))],
      ["discovered / skipped",
       `${universe.discovered_count} / ${universe.skipped_count}`],
      // 要求：区分“查看当前标的”与“修改运行的标的集合”；后者运行中不支持 ⇒ 显式说明原因。
      ["traded symbol (this run)",
       `${escapeHtml(tradedSymbol)} <span class="muted">（固定于启动参数 \`--symbol\`；运行中不可改）</span>`],
      ["collection set",
       `<span class="muted">由启动配置 \`market.universe.symbols\` 决定，本页只读：` +
       `运行中修改会改变采集/存储 Owner 与已落盘分片的归属，故不提供。要增减标的：修改配置后重启进程。</span>`],
    ]),
  ) + section("Symbols", table(
    ["symbol", "product", "quote", "exchange status", "collection", "last event (ts)", "messages"],
    rows_)) + section("Skipped", (universe.skipped || []).length
      ? table(["symbol", "reason"], universe.skipped.map((s) => [
          escapeHtml(s.symbol), escapeHtml(s.reason)]))
      : rows([["skipped", "none"]]));
}

export async function renderSymbolDetail(symbol) {
  const [status, trades] = await Promise.all([
    fetchOrUnavailable(`/api/v1/market/symbols/${encodeURIComponent(symbol)}/status`),
    fetchOrUnavailable(`${DATASET_PATH}/trade/${encodeURIComponent(symbol)}?limit=50`),
  ]);
  if (status.unavailable) {
    return section(`Symbol ${escapeHtml(symbol)}`, rows([
      ["collection", `<span class="unknown">UNAVAILABLE (${escapeHtml(status.unavailable)})</span>`],
    ]));
  }
  const statusRows = rows([
    ["status", statusBadge(status.status)],
    ["subscribed", String(Boolean(status.subscribed))],
    ["healthy", String(Boolean(status.healthy))],
    ["messages", String(status.message_count ?? 0)],
    ["states", String(status.state_count ?? 0)],
    ["resyncs", String(status.resync_count ?? 0)],
    ["last event (exchange ts)", status.last_event_exchange_ts != null
      ? String(status.last_event_exchange_ts) : "UNKNOWN"],
    ["market generation", String(status.market_generation ?? 0)],
    ["last error", status.last_error
      ? `<span class="bad">${escapeHtml(status.last_error)}</span>` : "none"],
  ]);
  let datasetBlock;
  if (trades.unavailable) {
    datasetBlock = section("Recorded trades (shared dataset)",
      rows([["dataset", `<span class="unknown">UNAVAILABLE (${escapeHtml(trades.unavailable)})</span>`]]));
  } else {
    const records = trades.records || [];
    datasetBlock = section("Recorded trades (shared dataset)",
      rows([
        ["source", escapeHtml(trades.source)],
        ["records (window)", String(records.length)],
        ["partitions", String((trades.partitions || []).length)],
      ]) + (records.length
        ? table(["exchange_ts", "price", "qty", "source"], records.slice(-20).reverse().map((r) => [
            String(r.exchange_ts), String(r.payload.price), String(r.payload.qty), escapeHtml(r.source)]))
        : rows([["records", '<span class="unknown">no recorded trades yet</span>']]))
      + section("Partitions / gaps", table(
          ["day", "rows", "first_ts", "last_ts", "bytes"],
          (trades.partitions || []).map((p) => [p.day, p.rows, String(p.first_ts), String(p.last_ts), p.bytes]))
        + ((trades.gaps || []).length
          ? rows(trades.gaps.map((g) => ["gap", `${g.day} (${g.reason})`])) : "")));
  }
  return section(`Symbol ${escapeHtml(symbol)} · collection status`, statusRows) + datasetBlock +
    `<section><h2>navigation</h2><a href="#/market/universe">← back to universe</a></section>`;
}
