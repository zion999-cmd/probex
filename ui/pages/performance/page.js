/** Performance：以 Run 为第一组织单位（§4）。F-19：run 列表按分页读取。 */
import { ENDPOINTS, fetchJson, fetchOrUnavailable } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Performance";
export const slug = "performance";

const PAGE_LIMIT = 10;

export async function render(rest = []) {
  // F4：`#/performance/run/<run_id>` 选择 run；默认选最新一个。
  const requested = rest[0] && !["runs", "metrics"].includes(rest[0]) ? String(rest[0]) : null;
  const runsPayload = await fetchOrUnavailable(`${ENDPOINTS.runs}?limit=${PAGE_LIMIT}`);
  if (runsPayload.unavailable) {
    return section("Runs (run is the unit)", rows([["run registry",
      `<span class="unknown">UNKNOWN (${escapeHtml(runsPayload.unavailable)})</span>`]]));
  }
  const runs = runsPayload.runs || [];
  const selected = (requested && runs.some((run) => run.run_id === requested))
    ? requested : (runs[0] ? runs[0].run_id : null);
  const pagination = runsPayload.pagination || {};
  const runList = table(["run_id", "mode", "status", "started_at", "config id", "fingerprint", "select", "review"],
    runs.map((run) => [escapeHtml(run.run_id || ""), escapeHtml((run.runtime || {}).mode || ""),
      escapeHtml(run.status || ""), String(run.started_at ?? ""), fact(run.config_id),
      fact(run.config_fingerprint),
      run.run_id === selected ? '<span class="known">selected</span>'
        : `<a href="#/performance/run/${encodeURIComponent(run.run_id)}">select</a>`,
      `<a href="#/market/run-review/${encodeURIComponent(run.run_id)}">run review</a> · ` +
      `<a href="#/activity/run/${encodeURIComponent(run.run_id)}">activity</a>`]));
  const paginationBlock = rows([
    ["offset / limit", `${escapeHtml(String(pagination.offset ?? 0))} / ${escapeHtml(String(pagination.limit ?? PAGE_LIMIT))}`],
    ["total", escapeHtml(String(pagination.total ?? runs.length))],
    ["has_more", escapeHtml(String(Boolean(pagination.has_more)))],
    ["next_offset", pagination.next_offset === null || pagination.next_offset === undefined
      ? "none" : `<a href="#/performance/runs/${escapeHtml(String(pagination.next_offset))}">${escapeHtml(String(pagination.next_offset))}</a>`],
    ["paginated list (F-19)", '<a href="#/performance/runs">open the bounded run list</a>'],
    ["run → activity", "select a run, then drill into its causal trace in Activity"],
  ]);
  // F4：从 durable run 读取真实 summary（未接线/未知 run ⇒ 明确 UNKNOWN，不是空报告）
  const summary = selected
    ? await fetchOrUnavailable(`${ENDPOINTS.runSummary}?run=${encodeURIComponent(selected)}`)
    : { unavailable: "no runs recorded yet" };
  const metrics = summary.unavailable
    ? rows([["run summary", `<span class="unknown">UNKNOWN (${escapeHtml(summary.unavailable)})</span>`]])
    : factRows(((summary.run_summary || {}).metrics) || {});
  const definitions = await fetchOrUnavailable(ENDPOINTS.metrics);
  const definitionTable = definitions.unavailable ? "" : table(["metric", "formula", "UNKNOWN condition"],
    (definitions.definitions || []).map((d) => [escapeHtml(d.name), escapeHtml(d.formula),
      escapeHtml(d.unknown_condition)]));
  let compare = rows([["compare", "requires two runs"]]);
  if (runs.length >= 2) {
    const comparison = await fetchOrUnavailable(
      `${ENDPOINTS.runCompare}?left=${encodeURIComponent(runs[0].run_id)}&right=${encodeURIComponent(runs[1].run_id)}`);
    compare = comparison.unavailable
      ? rows([["compare", `<span class="unknown">UNKNOWN (${escapeHtml(comparison.unavailable)})</span>`]])
      : table(["metric", "left", "right", "delta"],
          ((comparison.comparison || {}).metrics || []).map((m) => [escapeHtml(m.name), fact(m.left),
            fact(m.right), fact(m.delta)]));
  }
  const timeline = await fetchOrUnavailable(ENDPOINTS.portfolioTimeline);
  const timelineBlock = timeline.unavailable
    ? rows([["equity timeline", `<span class="unknown">UNKNOWN (${escapeHtml(timeline.unavailable)})</span>`]])
    : table(["ts", "equity", "balance", "position", "exposure (total)"],
        (timeline.timeline.points || []).map((p) => [String(p.ts), fact(p.equity), fact(p.balance),
          fact(p.position_qty), fact(p.exposure_total)]));
  return section("Runs (run is the unit)", runList) +
    section("Run selector", rows([
      ["selected run", selected ? `<code>${escapeHtml(selected)}</code>` : "none"],
      ["select", runs.map((run) => `<a href="#/performance/run/${encodeURIComponent(run.run_id)}">${escapeHtml(run.run_id)}</a>`).join(" · ") || "none"],
    ])) +
    section("Pagination (F-19: bounded list)", paginationBlock) +
    section("Equity / exposure timeline (G3)", timelineBlock) +
    section(`Latest run metrics${selected ? ` · ${escapeHtml(selected)}` : ""}`, metrics) +
    section("Compare", compare) +
    section("Metric contract (definitions)", definitionTable) +
    section("Run identity", factRows(((summary.run_summary || {}).run) || {}));
}
