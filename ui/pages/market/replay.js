/** Replay controls：只 POST 到 /api/v1/replay/*（local replay session control）。
 *
 * closure slice：支持 `onCommand` 回调，让 K 线/heatmap 与 replay cursor 时间同步。
 */
import { postReplay } from "/ui/client/api.js";
import { escapeHtml, rows } from "/ui/client/render.js";

export const SPEEDS = [0.25, 0.5, 1, 2, 5, 10];

export function replayControls(container, options = {}) {
  container.innerHTML = `
    <div class="row"><span class="k">controls</span><span class="v">
      <button data-verb="play">play</button>
      <button data-verb="pause">pause</button>
      <button data-verb="step">step</button>
      ${SPEEDS.map((speed) => `<button data-speed="${speed}">${speed}x</button>`).join("")}
      <button data-seek="last">seek → now</button>
    </span></div>
    <div id="replay-status" class="row"><span class="k">status</span><span class="v">idle</span></div>
  `;
  const status = container.querySelector("#replay-status .v");
  const run = async (verb, payload = {}) => {
    try {
      const body = await postReplay(verb, payload);
      const control = body.control || {};
      const cursor = control.last_ts || control.timestamp || "";
      status.innerHTML = `<span class="known">${escapeHtml(verb)} @ ${escapeHtml(String(control.speed ?? ""))}x` +
        `${cursor ? ` · cursor=${escapeHtml(String(cursor))}` : ""}</span>`;
      if (typeof options.onCommand === "function") await options.onCommand(verb, control);
    } catch (error) {
      status.innerHTML = `<span class="unknown">${escapeHtml(String(error))}</span>`;
    }
  };
  container.querySelectorAll("button[data-verb]").forEach((button) => {
    button.addEventListener("click", () => run(button.dataset.verb));
  });
  container.querySelectorAll("button[data-speed]").forEach((button) => {
    button.addEventListener("click", () => run("speed", { speed: Number(button.dataset.speed) }));
  });
  const seek = container.querySelector("button[data-seek]");
  if (seek) seek.addEventListener("click", () => run("seek"));
}
