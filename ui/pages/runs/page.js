/** Runs detail view（P0001.10.2 / F-19）：有界分页读取 run 列表。 */
import { ENDPOINTS, fetchOrUnavailable } from "/ui/client/api.js";
import { escapeHtml, fact, rows, section, table } from "/ui/client/render.js";

export const title = "Runs";
export const slug = "runs";

const PAGE_LIMIT = 20;

export async function render(rest = []) {
  const requested = Number.parseInt(rest[0] ?? "0", 10);
  const offset = Number.isFinite(requested) && requested >= 0 ? requested : 0;
  const payload = await fetchOrUnavailable(`${ENDPOINTS.runs}?limit=${PAGE_LIMIT}&offset=${offset}`);
  if (payload.unavailable) {
    return section("Runs", rows([["run registry",
      `<span class="unknown">UNKNOWN (${escapeHtml(payload.unavailable)})</span>`]]));
  }
  const runs = payload.runs || [];
  const pagination = payload.pagination || {};
  const list = table(["run_id", "mode", "status", "started_at", "config fingerprint", "links"],
    runs.map((run) => [escapeHtml(run.run_id || ""), escapeHtml((run.runtime || {}).mode || ""),
      escapeHtml(run.status || ""), String(run.started_at ?? ""), fact(run.config_fingerprint),
      `<a href="#/market/run-review/${encodeURIComponent(run.run_id)}">run review</a> · ` +
      `<a href="#/activity/run/${encodeURIComponent(run.run_id)}">activity</a>`]));
  const prev = offset - PAGE_LIMIT >= 0
    ? `<a href="#/performance/runs/${offset - PAGE_LIMIT}">prev</a>` : "none";
  const next = pagination.has_more && pagination.next_offset !== null && pagination.next_offset !== undefined
    ? `<a href="#/performance/runs/${pagination.next_offset}">next</a>` : "none";
  const meta = rows([
    ["offset / limit", `${escapeHtml(String(pagination.offset ?? offset))} / ${escapeHtml(String(pagination.limit ?? PAGE_LIMIT))}`],
    ["total", escapeHtml(String(pagination.total ?? runs.length))],
    ["has_more", escapeHtml(String(Boolean(pagination.has_more)))],
    ["previous", prev], ["next", next],
    ["bounds", "the list is bounded by the Product API (limit has a safe maximum)"],
  ]);
  return section("Runs (bounded page)", list) + section("Pagination", meta);
}
