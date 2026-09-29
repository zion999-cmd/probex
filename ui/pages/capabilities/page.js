/** Capabilities（P0001.11 §5）：机器可读能力清单（READ vs WRITE=unavailable_by_design）。 */
import { ENDPOINTS, fetchJson } from "/ui/client/api.js";
import { escapeHtml, rows, section, table } from "/ui/client/render.js";

export const title = "Capabilities";
export const slug = "capabilities";

export async function render() {
  const manifest = await fetchJson(ENDPOINTS.capabilities);
  const api = manifest.api || {};
  const cli = manifest.cli || {};
  return section("Write surface", rows([
    ["api.write", `<span class="bad">${escapeHtml(String(api.write))}</span>`],
    ["unavailable actions", escapeHtml((manifest.unavailable_actions || []).join(", "))],
  ])) +
  section("API read endpoints", table(["path"], (api.read || []).map((path) => [escapeHtml(path)]))) +
  section("CLI commands", table(["command", "endpoint"],
    Object.entries(cli.commands || {}).map(([name, path]) => [escapeHtml(name), escapeHtml(path)]))) +
  section("Exit codes", table(["code", "meaning"],
    Object.entries(cli.exit_codes || {}).map(([code, meaning]) => [escapeHtml(code), escapeHtml(meaning)])));
}
