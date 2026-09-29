/** Assistant Drawer（P0001.12.3 §7）：五个 Surface 的全局能力，不是第六个一级页面。 */
import { escapeHtml } from "/ui/client/render.js";
import { currentSelection } from "/ui/assistant/context.js";
import { invoke, loadSuggestions, renderActions } from "/ui/assistant/actions.js";

export const DRAWER_ID = "assistant-drawer";

export function mountAssistant(container) {
  const drawer = document.createElement("aside");
  drawer.id = DRAWER_ID;
  drawer.innerHTML = `
    <header><strong>Probex Assistant</strong>
      <button id="assistant-toggle" title="collapse">–</button></header>
    <div id="assistant-body">
      <div class="row"><span class="k">context</span><span class="v">…</span></div>
      <div id="assistant-actions"></div>
      <div id="assistant-status"></div>
    </div>`;
  container.appendChild(drawer);
  drawer.querySelector("#assistant-toggle").addEventListener("click", () => {
    drawer.classList.toggle("collapsed");
  });
  refreshAssistant();
  window.addEventListener("hashchange", refreshAssistant);
  return drawer;
}

export async function refreshAssistant() {
  const body = document.getElementById("assistant-body");
  if (!body) return;
  const selection = currentSelection();
  const contextRow = body.querySelector(".row .v");
  const actionsHost = document.getElementById("assistant-actions");
  try {
    const { context, suggested } = await loadSuggestions(selection);
    contextRow.innerHTML = `${escapeHtml(context.surface)} · ${escapeHtml(context.runtime.mode)} · ` +
      `${escapeHtml(context.runtime.runtime_id)} · blockers=${context.active_blockers.length}`;
    actionsHost.innerHTML = renderActions(suggested);
    actionsHost.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const status = document.getElementById("assistant-status");
        let outcome = await invoke(button.dataset.action, { surface: selection.surface });
        if (outcome.status === "CONFIRMATION_REQUIRED") {
          status.innerHTML = `<span class="unknown">confirmation required</span>`;
          outcome = await invoke(button.dataset.action, { surface: selection.surface,
                                                          confirmation: outcome.confirmation_id });
        }
        const cls = outcome.status === "SUCCEEDED" ? "known" : "bad";
        status.innerHTML = `<span class="${cls}">${escapeHtml(outcome.status)}</span> ` +
          `${escapeHtml(outcome.reason_code || "")}`;
      });
    });
  } catch (error) {
    contextRow.innerHTML = `<span class="unknown">UNKNOWN (${escapeHtml(String(error))})</span>`;
    actionsHost.innerHTML = "";
  }
}
