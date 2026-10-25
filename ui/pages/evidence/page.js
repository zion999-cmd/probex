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
  // 契约：必须同时给出 kind 与 identity（`#/activity/evidence/<kind>/<identity>`）。
  // 只有一段时**不猜测**：显式说明原因，而不是抛错或查错端点。
  const hasFactTarget = Boolean(kind && identity);
  let factsBlock = rows([["raw facts",
    kind && !identity
      ? `<span class="unknown">UNKNOWN (需要 kind + identity：当前只给了 ${escapeHtml(kind)}；` +
        `从 Activity 的 order / fill / execution_event 身份单元格进入)</span>`
      : "open Activity and click an order / fill identity to drill down"]]);
  if (hasFactTarget) {
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
    section(`Raw facts (G2)${hasFactTarget ? ` · ${escapeHtml(kind)}:${escapeHtml(identity)}` : ""}`, factsBlock) +
    `<section id="evidence-blockers">` +
      `<h2>Blockers</h2>` + rows([
      ["readiness blockers", escapeHtml((evidence.readiness_blockers || []).join(", ") || "none")],
      ["risk rejects", escapeHtml((evidence.risk_rejects || []).join(", ") || "none")],
    ]) + `</section>`;
}

/** P0018：全局 blocker 摘要点击后落到本页 Blockers 段（不重构 Activity，仅锚定）。 */
export async function mount() {
  try {
    const { hashQuery } = await import("/ui/app/navigation.js");
    if (hashQuery().get("focus") !== "blockers") return;
    const target = document.getElementById("evidence-blockers");
    if (target) {
      target.scrollIntoView({ block: "center" });
      target.classList.add("focus-flash");
    }
  } catch { /* 锚定失败不阻塞页面 */ }
}
