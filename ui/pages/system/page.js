/** System：为什么能/不能运行（§5）。二级：Health / Risk / Readiness / Execution / Configuration / Capabilities。 */
import { ENDPOINTS, fetchJson, fetchOrUnavailable, fetchSnapshot } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, rows, section, table } from "/ui/client/render.js";

export const title = "System";
export const slug = "system";

export const SECTIONS = ["health", "risk", "readiness", "execution", "configuration", "capabilities"];

export async function render(rest = []) {
  const active = SECTIONS.includes(rest[0]) ? rest[0] : "health";
  const snapshot = await fetchSnapshot();
  const nav = SECTIONS.map((name) =>
    `<a href="#/system/${name}" class="${name === active ? "active" : ""}">${escapeHtml(name)}</a>`).join(" · ");
  const header = section("System surfaces", nav);
  if (active === "health") {
    return header + section("Health", factRows(snapshot.health)) +
      section("Market / stream", rows([
        ["market healthy", fact(snapshot.market.healthy)],
        ["market tradeable", fact(snapshot.market.tradeable)],
        ["private stream", fact(snapshot.health.private_stream_state)],
        ["clock offset (ms)", fact(snapshot.health.clock_offset_ms)],
        ["uptime (ms)", fact(snapshot.health.uptime_ms)],
      ]));
  }
  if (active === "risk") return header + section("Risk", factRows(snapshot.risk)) +
      section("Rejects (reason codes)", rows((snapshot.risk.rejects || []).map((r, i) => [`reject ${i + 1}`,
        `<span class="bad">${escapeHtml(r)}</span>`])) || rows([["rejects", "none"]]));
  if (active === "readiness") return header + section("Readiness", factRows(snapshot.readiness)) +
      section("Blockers", rows((snapshot.readiness.reasons || []).map((r, i) => [`reason ${i + 1}`,
        `<span class="bad">${escapeHtml(r)}</span>`])) || rows([["reasons", "none"]]));
  if (active === "execution") {
    const execution = snapshot.execution || {};
    return header + section("Execution health (P0001.13 slot)", rows([
      ["active orders", String((execution.active_orders || []).length)],
      ["uncertain exposure", fact(execution.uncertain_exposure)],
      ["open order exposure", fact(execution.open_order_exposure)],
      ["unknown exposure flag", String(Boolean(execution.has_unknown_exposure))],
      ["unknown submit count", fact(execution.unknown_submit_count)],
      ["unknown cancel count", fact(execution.unknown_cancel_count)],
      ["rate limits", '<span class="unknown">UNKNOWN (P0001.13)</span>'],
      ["venue limits", '<span class="unknown">UNKNOWN (P0001.13)</span>'],
      ["latency", '<span class="unknown">UNKNOWN (P0001.13)</span>'],
      ["reconciliation", '<span class="unknown">UNKNOWN (P0001.13)</span>'],
    ]));
  }
  if (active === "configuration") {
    const config = snapshot.config || {};
    return header + section("Configuration (read-only)", rows([
      ["config_id", fact(config.config_id)],
      ["fingerprint", fact(config.fingerprint)],
      ["created_at", fact(config.created_at)],
      ["sources", escapeHtml(JSON.stringify(config.sources || {}))],
      ["secret references", escapeHtml((config.secret_refs || []).join(", ") || "none")],
    ])) + section("Resolved entries (non-sensitive only)", table(["name", "source", "value", "secret ref"],
      (config.entries || []).map((e) => [escapeHtml(e.name), escapeHtml(e.source), fact(e.value),
        escapeHtml(e.secret_ref || "")])));
  }
  const capabilities = await fetchOrUnavailable(ENDPOINTS.capabilities);
  if (capabilities.unavailable) {
    return header + section("Capabilities",
      rows([["manifest", `<span class="unknown">UNKNOWN (${escapeHtml(capabilities.unavailable)})</span>`]]));
  }
  const api = capabilities.api || {};
  return header +
    section("Capabilities", rows([
      ["api.write", `<span class="bad">${escapeHtml(String(api.write))}</span>`],
      ["simulation control", escapeHtml((api.simulation_control || []).join(", "))],
      ["unavailable actions", escapeHtml((capabilities.unavailable_actions || []).join(", "))],
    ])) +
    section("Read endpoints", table(["path"], (api.read || []).map((p) => [escapeHtml(p)])));
}
