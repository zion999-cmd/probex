/** System：为什么能/不能运行（§5）。二级：Health / Risk / Readiness / Execution / Configuration / Capabilities。 */
import { ENDPOINTS, fetchJson, fetchOrUnavailable, fetchSnapshot, reasonCatalog } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, reasonCell, rows, section, table } from "/ui/client/render.js";

export const title = "System";
export const slug = "system";

export const SECTIONS = ["health", "risk", "readiness", "execution", "configuration", "capabilities"];

async function executionSections() {
  const [limits, rateLimits, latency, reconciliation, anomalies] = await Promise.all([
    fetchOrUnavailable(ENDPOINTS.executionLimits), fetchOrUnavailable(ENDPOINTS.executionRateLimits),
    fetchOrUnavailable(ENDPOINTS.executionLatency), fetchOrUnavailable(ENDPOINTS.executionReconciliation),
    fetchOrUnavailable(ENDPOINTS.executionAnomalies),
  ]);
  const unknownRow = (label, payload) => rows([[label,
    payload.unavailable ? `<span class="unknown">UNKNOWN (${escapeHtml(payload.unavailable)})</span>` : "ok"]]);
  const limitsRows = limits.unavailable ? unknownRow("venue limits", limits)
    : factRows(limits.limits);
  const rateRows = rateLimits.unavailable ? unknownRow("rate limits", rateLimits)
    : factRows({ request_budget: rateLimits.governor.request.remaining,
                 order_budget: rateLimits.governor.order.remaining,
                 request_status: rateLimits.governor.request.status,
                 order_status: rateLimits.governor.order.status,
                 allows_new_exposure: rateLimits.governor.allows_new_exposure,
                 allows_de_risking: rateLimits.governor.allows_de_risking });
  const latencyRows = latency.unavailable ? unknownRow("latency", latency)
    : table(["stage", "status", "samples", "p50", "p95", "budget_ms"],
        latency.latency.stages.map((stage_) => [escapeHtml(stage_.stage), escapeHtml(stage_.status),
          String(stage_.sample_count), fact(stage_.p50), fact(stage_.p95), fact(stage_.budget_ms)]));
  const reconciliationRows = reconciliation.unavailable ? unknownRow("reconciliation", reconciliation)
    : factRows(reconciliation.reconciliation);
  const anomalyRows = anomalies.unavailable ? unknownRow("anomalies", anomalies)
    : table(["owner", "reason", "severity", "source"],
        anomalies.blockers.map((b) => [escapeHtml(b.owner), escapeHtml(b.reason_code),
          escapeHtml(b.severity), escapeHtml(b.source_ref)]));
  return section("Venue Limits", limitsRows) + section("Rate Limits", rateRows) +
    section("Latency", latencyRows) + section("Reconciliation", reconciliationRows) +
    section("Execution anomalies", anomalyRows);
}

export async function render(rest = []) {
  const active = SECTIONS.includes(rest[0]) ? rest[0] : "health";
  const snapshot = await fetchSnapshot();
  const catalog = await reasonCatalog();
  const nav = SECTIONS.map((name) =>
    `<a href="#/system/${name}" class="${name === active ? "active" : ""}">${escapeHtml(name)}</a>`).join(" · ");
  const header = section("System surfaces", nav);
  if (active === "health") {
    return header + section("Health", factRows(snapshot.health)) +
      section("Provider / accounting health (G4)", rows([
        ["prediction provider", fact(snapshot.health.prediction_provider)],
        ["accounting", fact(snapshot.health.accounting)],
      ])) +
      section("Runtime / loop", rows([
        ["state", fact(snapshot.health.runtime_state)],
        ["detail", fact(snapshot.health.runtime_detail)],
        ["quoting", fact(snapshot.health.runtime_quoting)],
        ["run id", fact(snapshot.health.runtime_run_id)],
        ["since (ms)", fact(snapshot.health.runtime_since_ms)],
      ])) +
      section("Market / stream", rows([
        ["market healthy", fact(snapshot.market.healthy)],
        ["market tradeable", fact(snapshot.market.tradeable)],
        ["private stream", fact(snapshot.health.private_stream_state)],
        ["clock offset (ms)", fact(snapshot.health.clock_offset_ms)],
        ["uptime (ms)", fact(snapshot.health.uptime_ms)],
      ])) +
      section("Blockers (reason codes + explanation)", table(["owner", "reason code", "severity", "message"],
        (snapshot.blockers || []).map((b) => [escapeHtml(b.owner), reasonCell(b.reason_code, catalog),
          escapeHtml(b.severity), escapeHtml(b.message)])));
  }
  if (active === "risk") {
    const rejects = (snapshot.risk.rejects || []).length
      ? table(["reason code (raw + human)"], snapshot.risk.rejects.map((r) => [reasonCell(r, catalog)]))
      : rows([["rejects", "none"]]);
    return header + section("Risk", factRows(snapshot.risk)) +
      section("Rejects (reason codes)", rejects);
  }
  if (active === "readiness") {
    const reasons = (snapshot.readiness.reasons || []).length
      ? table(["reason code (raw + human)"], snapshot.readiness.reasons.map((r) => [reasonCell(r, catalog)]))
      : rows([["reasons", "none"]]);
    return header + section("Authority facts (G5)", rows([
      ["kind", fact(snapshot.readiness.authority_kind)],
      ["issued_at_ms", fact(snapshot.readiness.authority_issued_at_ms)],
      ["expires_at_ms", fact(snapshot.readiness.authority_expires_at_ms)],
      ["recovery generation", fact(snapshot.readiness.authority_recovery_generation)],
      ["market generation", fact(snapshot.readiness.authority_market_generation)],
    ])) + section("Readiness", factRows(snapshot.readiness)) +
      section("Blockers", reasons) +
      section("Details", (snapshot.readiness.details || []).length
        ? `<pre>${escapeHtml((snapshot.readiness.details || []).join("\n"))}</pre>`
        : rows([["details", "none"]]));
  }
  if (active === "execution") {
    const execution = snapshot.execution || {};
    return header + await executionSections() + section("Execution health (summary)", rows([
      ["active orders", String((execution.active_orders || []).length)],
      ["uncertain exposure", fact(execution.uncertain_exposure)],
      ["open order exposure", fact(execution.open_order_exposure)],
      ["unknown exposure flag", String(Boolean(execution.has_unknown_exposure))],
      ["unknown submit count", fact(execution.unknown_submit_count)],
      ["unknown cancel count", fact(execution.unknown_cancel_count)],
    ]));
  }
  if (active === "configuration") {
    const config = snapshot.config || {};
    return header + section("Configuration (read-only)", rows([
      ["config_id", fact(config.config_id)],
      ["fingerprint", fact(config.fingerprint)],
      ["created_at", fact(config.created_at)],
      ["sources (CLI > ENV > FILE > CONSTRUCTOR)", escapeHtml(JSON.stringify(config.sources || {}))],
      ["secret references (names only)", escapeHtml((config.secret_refs || []).join(", ") || "none")],
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
  const cli = capabilities.cli || {};
  return header +
    section("Capabilities", rows([
      ["api.write", `<span class="bad">${escapeHtml(String(api.write))}</span>`],
      ["simulation control", escapeHtml((api.simulation_control || []).join(", "))],
      ["unavailable actions", escapeHtml((capabilities.unavailable_actions || []).join(", "))],
    ])) +
    section("Read endpoints", table(["path"], (api.read || []).map((p) => [escapeHtml(p)]))) +
    section("CLI commands", table(["command", "endpoint"],
      Object.entries(cli.commands || {}).map(([name, path]) => [escapeHtml(name), escapeHtml(path)]))) +
    section("Exit codes", table(["code", "meaning"],
      Object.entries(cli.exit_codes || {}).map(([code, meaning]) => [escapeHtml(code), escapeHtml(meaning)])));
}
