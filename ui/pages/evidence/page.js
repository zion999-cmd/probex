/** Evidence detail view（P0001.10.2 / F-16）：因果链 + G2 raw facts drill-down（有界）。 */
import { ENDPOINTS, fetchJson, fetchOrUnavailable, reasonCatalog } from "/ui/client/api.js";
import { escapeHtml, fact, reasonCell, rows, section, table } from "/ui/client/render.js";

export const title = "Evidence";
export const slug = "evidence";

export async function render(rest = []) {
  const { evidence } = await fetchJson(ENDPOINTS.evidence);
  const catalog = await reasonCatalog();
  const trace = table(["ts", "stage", "identity", "outcome", "reason code", "detail"],
    (evidence.trace || []).map((t) => [
      t.ts && t.ts.known ? String(t.ts.value) : '<span class="unknown">UNKNOWN</span>',
      escapeHtml(t.stage), fact(t.identity), escapeHtml(t.outcome),
      reasonCell(t.reason_code && t.reason_code.known ? t.reason_code.value : null, catalog),
      escapeHtml(t.detail || "")]));

  const [kind, identity] = rest;
  let factsBlock = rows([["raw facts", "open Activity and click an order / fill identity to drill down"]]);
  if (kind && identity) {
    const payload = await fetchOrUnavailable(
      `${ENDPOINTS.facts}/${encodeURIComponent(kind)}/${encodeURIComponent(identity)}`);
    if (payload.unavailable) {
      factsBlock = rows([["raw facts", `<span class="unknown">UNKNOWN (${escapeHtml(payload.unavailable)})</span>`]]);
    } else {
      const view = payload.fact || {};
      factsBlock = table(["field", "value"],
        (view.facts || []).map(([name, value]) => [escapeHtml(name), fact(value)])) +
        rows([
          ["identity", `${escapeHtml(view.kind || "")}:${escapeHtml(view.identity || "")}`],
          ["truncated fields", escapeHtml((view.truncated_fields || []).join(", ") || "none")],
          ["notes", escapeHtml((view.notes || []).join("; ") || "none")],
        ]);
    }
  }
  return section("Decision trace", trace) +
    section(`Raw facts (G2)${kind ? ` · ${escapeHtml(kind)}:${escapeHtml(identity)}` : ""}`, factsBlock) +
    section("Blockers", rows([
      ["readiness blockers", escapeHtml((evidence.readiness_blockers || []).join(", ") || "none")],
      ["risk rejects", escapeHtml((evidence.risk_rejects || []).join(", ") || "none")],
    ]));
}
