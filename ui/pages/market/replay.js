/** Replay controls：只 POST 到 /api/v1/replay/*（local replay session control）。 */
import { postReplay } from "/ui/client/api.js";
import { escapeHtml, rows } from "/ui/client/render.js";

export const SPEEDS = [0.25, 0.5, 1, 2, 5, 10];

export function replayControls(container) {
  container.innerHTML = `
    <div class="row"><span class="k">controls</span><span class="v">
      <button data-verb="play">play</button>
      <button data-verb="pause">pause</button>
      <button data-verb="step">step</button>
    </span></div>
    ${rows([["speeds", SPEEDS.map((speed) => `${speed}x`).join(" · ")], ["seek", "restart replay + fast-forward"]])}
    <div id="replay-status" class="row"><span class="k">status</span><span class="v">idle</span></div>
  `;
  const status = container.querySelector("#replay-status .v");
  container.querySelectorAll("button[data-verb]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const payload = await postReplay(button.dataset.verb, {});
        status.innerHTML = `<span class="known">${escapeHtml(payload.command.verb)} @ ${payload.control.speed}x</span>`;
      } catch (error) {
        status.innerHTML = `<span class="unknown">${escapeHtml(String(error))}</span>`;
      }
    });
  });
}
