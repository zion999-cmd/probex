/** Assistant 动作面板：按钮**只能**来自 Action Manifest（AI 不得自创 API）。 */
import { ENDPOINTS, fetchJson } from "/ui/client/api.js";
import { escapeHtml } from "/ui/client/render.js";

export const CONFIRMABLE_LEVELS = ["L2_RUNTIME"];

export async function loadSuggestions(context) {
  const query = new URLSearchParams({ surface: context.surface, ...context.selection });
  const payload = await fetchJson(`${ENDPOINTS.assistantContext}?${query.toString()}`);
  return { context: payload.context, suggested: payload.suggested_actions || [] };
}

export async function invoke(actionId, { parameters = {}, confirmation = null, surface = "monitor" } = {}) {
  const body = { parameters, requested_by: "ui-assistant", surface };
  if (confirmation) body.confirmation = confirmation;
  const response = await fetch(`${ENDPOINTS.actions}/${actionId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  return payload.action || { status: "UNKNOWN", reason_code: `HTTP_${response.status}` };
}

export function renderActions(suggested) {
  if (!suggested.length) return '<div class="row"><span class="k">actions</span><span class="v unknown">none available</span></div>';
  return suggested.map((entry) =>
    `<button data-action="${escapeHtml(entry.action_id)}" data-confirm="${entry.confirmation_required}">` +
    `${escapeHtml(entry.action_id)}${entry.confirmation_required ? " (confirm)" : ""}</button>`).join(" ");
}
