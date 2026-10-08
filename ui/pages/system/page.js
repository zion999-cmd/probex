/** System：为什么能/不能运行（§5）。二级：Health / Risk / Readiness / Execution / Configuration / Capabilities。 */
import { ENDPOINTS, fetchJson, fetchOrUnavailable, fetchSnapshot, reasonCatalog } from "/ui/client/api.js";
import { escapeHtml, fact, factRows, jsonDetail, reasonCell, rows, section, table, valueView } from "/ui/client/render.js";
import { mountOpsCharts, opsChartsSection } from "/ui/system/ops_charts.js";

let lastExecution = null;
let lastSnapshot = null;

export const title = "System";
export const slug = "system";

export const SECTIONS = ["health", "risk", "readiness", "execution", "instruments", "connections",
                         "configuration", "capabilities", "ops"];

/** F3/F-12/F-15：operational posture。
 *
 * 默认渲染"人看得懂的摘要"（标量表格 + 嵌套折叠）；raw JSON 只在 detail 里，
 * 绝不再出现 `[object Object]`。
 */
function postureSection(title, factValue, label) {
  if (!factValue || factValue.known === false) {
    return section(title, rows([[label, fact(factValue)]]));
  }
  const raw = factValue.value;
  return section(title, valueView(raw) + (raw && typeof raw === "object" ? jsonDetail(raw, `${label} raw`) : ""));
}

function opsSection(ops) {
  return section("Process liveness / health split", rows([
    ["process live", fact(ops.process_live)],
    ["runtime state", fact(ops.runtime_state)],
    ["runtime detail", fact(ops.runtime_detail)],
    ["trade readiness", fact(ops.trade_readiness)],
    ["readiness reasons", escapeHtml((ops.trade_readiness_reasons || []).join(", ") || "none")],
    ["execution health", fact(ops.execution_health)],
    ["operational warning", fact(ops.operational_warning)],
  ])) +
    postureSection("Network / auth posture", ops.network, "network") +
    postureSection("Logging posture", ops.logging, "logging") +
    postureSection("Retention posture", ops.retention, "retention");
}

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
  lastExecution = { latency, rateLimits, anomalies };
  return section("Venue Limits", limitsRows) + section("Rate Limits", rateRows) +
    section("Latency", latencyRows) + section("Reconciliation", reconciliationRows) +
    section("Execution anomalies", anomalyRows) +
    `<section class="wide"><h2>Execution charts</h2>${opsChartsSection()}</section>`;
}

function venueRules() {
  return (lastSnapshot && lastSnapshot.config && lastSnapshot.config.venue_rules) || null;
}

function truthy(source, key, fallbackKey) {
  if (!source) return '<span class="unknown">UNKNOWN</span>';
  const value = source[key];
  if (fallbackKey && source[fallbackKey] !== undefined && value !== undefined) {
    return `${escapeHtml(String(value))} / ${escapeHtml(String(source[fallbackKey]))}`;
  }
  return value === undefined || value === null ? '<span class="unknown">UNKNOWN</span>' : escapeHtml(String(value));
}

export async function render(rest = []) {
  const active = SECTIONS.includes(rest[0]) ? rest[0] : "health";
  const snapshot = await fetchSnapshot();
  lastSnapshot = snapshot;
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
        ["book health", fact(snapshot.health.book_health)],
        ["market healthy", fact(snapshot.health.market_healthy)],
        ["market tradeable", fact(snapshot.health.market_tradeable)],
        ["private stream", fact(snapshot.health.private_stream_state)],
        ["clock offset (ms)", fact(snapshot.health.clock_offset_ms)],
        ["uptime (ms)", fact(snapshot.health.uptime_ms)],
      ])) +
      section("Blockers (reason codes + explanation)", table(["owner", "reason code", "severity", "message"],
        (snapshot.blockers || []).map((b) => [escapeHtml(b.owner), reasonCell(b.reason_code, catalog),
          escapeHtml(b.severity), escapeHtml(b.message)])));
  }
  if (active === "instruments") {
    // P0001.15 §26：当前 instrument spec（capabilities / reference price policy / venue rules）
    const instrument = snapshot.instrument || {};
    const capabilities = instrument.capabilities && instrument.capabilities.known
      ? instrument.capabilities.value : null;
    const policy = instrument.reference_price_policy && instrument.reference_price_policy.known
      ? instrument.reference_price_policy.value : null;
    const capabilityRows = capabilities
      ? rows(Object.entries(capabilities).map(([key, value]) => [key, escapeHtml(String(value))]))
      : rows([["capabilities", '<span class="unknown">UNKNOWN</span>']]);
    const policyRows = policy
      ? rows([
        ["risk price types", escapeHtml((policy.risk_price_types || []).join(", "))],
        ["observable price types", escapeHtml((policy.observable_price_types || []).join(", "))],
        ["substitution allowed", String(policy.substitution_allowed)],
      ])
      : rows([["reference price policy", '<span class="unknown">UNKNOWN</span>']]);
    const specRows = rows([
      ["instrument id", fact(instrument.instrument_id)],
      ["symbol", fact(instrument.symbol)],
      ["asset class", fact(instrument.asset_class)],
      ["product type", fact(instrument.product_type)],
      ["base / quote / settle", `${fact(instrument.base_asset)} / ${fact(instrument.quote_asset)} / ${fact(instrument.settlement_asset)}`],
      ["price tick / qty step", `${fact(instrument.price_tick)} / ${fact(instrument.quantity_step)}`],
      ["min quantity / notional", `${fact(instrument.min_quantity)} / ${fact(instrument.min_notional)}`],
      ["production ready", fact(instrument.production_ready)],
    ]);
    return header + section("Instrument spec", specRows) +
      section("Capabilities (product semantics)", capabilityRows) +
      section("Reference price policy", policyRows) +
      section("Venue rules (authoritative for execution)", rows([
        ["tick / step (venue)", truthy(venueRules(), "tick_size", "step_size")],
        ["source", truthy(venueRules(), "source", null)],
      ]));
  }
  if (active === "connections") {
    // P0001.15 §26 / SC-28：两个 connector health **分开**显示，绝不合并成单一 Connected
    const market = snapshot.market_connector_health || {};
    const private_ = snapshot.private_connector_health || {};
    const venue = snapshot.venue || {};
    const marketExtras = market.extras && market.extras.known ? market.extras.value : {};
    const privateExtras = private_.extras && private_.extras.known ? private_.extras.value : {};
    const marketRows = rows([
      ["connector id", fact(market.connector_id)],
      ["connection state", fact(market.connection_state)],
      ["last event (ms)", fact(market.last_event_ms)],
      ["event age (ms)", fact(market.event_age_ms)],
      ["observed", fact(market.observed)],
      ["book health", fact(marketExtras.book_health)],
      ["reconnects", fact(marketExtras.reconnect_count)],
      ["resyncs", fact(marketExtras.resync_count)],
      ["detail", fact(market.detail)],
    ]);
    const privateRows = rows([
      ["connector id", fact(private_.connector_id)],
      ["connection state", fact(private_.connection_state)],
      ["user stream", private_.observed.known
        ? (private_.observed.value ? "observed" : '<span class="unknown">未观测到业务事件（≠ 没有成交）</span>')
        : '<span class="unknown">UNKNOWN</span>'],
      ["last private event (ms)", fact(private_.last_event_ms)],
      ["private event age (ms)", fact(private_.event_age_ms)],
      ["last order ack (ms)", fact(privateExtras.last_order_ack_ms)],
      ["clock offset (ms)", fact(snapshot.health.clock_offset_ms)],
      ["reconciliation", fact(privateExtras.reconciliation_state)],
      ["account freshness (ms)", fact(privateExtras.account_state_freshness_ms)],
      ["rate limit", fact(privateExtras.rate_limit_state)],
      ["latency samples", fact(snapshot.health.latency_samples)],
      ["detail", fact(private_.detail)],
    ]);
    return header + section("Venue", rows([
      ["venue id", fact(venue.venue_id)],
      ["venue type", fact(venue.venue_type)],
      ["environment", fact(venue.environment)],
    ])) + section("Market data connector", marketRows) +
      section("Private execution connector", privateRows) +
      section("Execution audit (last submit)", rows([
        ["classification", fact(snapshot.execution.last_submit_classification)],
        ["reason (redacted)", fact(snapshot.execution.last_submit_reason)],
      ])) +
      section("Note", rows([["separation",
        "market health 与 private health 独立：行情正常 ≠ 私有交易连接正常（不显示单一 Connected）"]]));
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
  if (active === "ops") {
    return header + opsSection(snapshot.ops || {});
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
      (config.entries || []).map((e) => [escapeHtml(e.name), escapeHtml(e.source), valueView(e.value),
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

export function mount(rest = []) {
  if (SECTIONS.includes(rest[0]) ? rest[0] === "execution" : true) {
    if (lastExecution) mountOpsCharts(lastExecution);
  }
}
