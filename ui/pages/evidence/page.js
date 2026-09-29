/** Evidence 页面（P0001.10.2 §1）：只消费 Product API 切片 `evidence`。 */
import { ENDPOINTS, fetchJson, fetchRunSummary, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "Evidence";
export const slug = "evidence";

export async function render() {
  const { evidence } = await fetchJson(ENDPOINTS.evidence);
  const trace = table(["stage", "identity", "outcome", "reason code", "detail"],
    (evidence.trace || []).map((t) => [escapeHtml(t.stage), fact(t.identity), escapeHtml(t.outcome),
      fact(t.reason_code), escapeHtml(t.detail || "")]));
  return section("Decision trace", trace) +
    section("Blockers", rows([
      ["readiness blockers", escapeHtml((evidence.readiness_blockers || []).join(", ") || "none")],
      ["risk rejects", escapeHtml((evidence.risk_rejects || []).join(", ") || "none")],
    ]));
}
