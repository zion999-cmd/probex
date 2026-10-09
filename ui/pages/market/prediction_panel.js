/**
 * Prediction 可视化（Market + Activity）。**不新增一级 Surface**。
 *
 * 无预测记录 / 无 horizon 分布时明确 UNKNOWN/STALE，绝不用中性 50% 冒充事实。
 */
import { escapeHtml, fact, rows, section, table } from "/ui/client/render.js";
import { barOption, chartsAvailable, mountChart } from "/ui/client/charts.js";

export const HORIZON_CATEGORIES = ["strong_down", "down", "flat", "up", "strong_up"];

function horizonLabel(ms) {
  if (!Number.isFinite(ms)) return "?";
  if (ms % 3_600_000 === 0) return `${ms / 3_600_000}h`;
  if (ms % 60_000 === 0) return `${ms / 60_000}m`;
  return `${Math.round(ms / 1000)}s`;
}

/** Market/Activity 共用的 prediction 摘要 + 多 horizon 面板。 */
function providerLabel(view) {
  if (!view || !view.provider || !view.provider.known) {
    return '<span class="unknown">UNKNOWN</span>';
  }
  const provider = escapeHtml(String(view.provider.value));
  if (view.is_local_trial && view.is_local_trial.known && view.is_local_trial.value) {
    return `<span class="warn">LOCAL_TRIAL</span> <span class="muted">本地试验规则，非真实模型</span>`;
  }
  return provider;
}

export function predictionSection(prediction, { decision = null, id = "prediction-horizons" } = {}) {
  const view = prediction || {};
  const horizons = view.horizons && view.horizons.known ? view.horizons.value : null;
  const linkage = rows([
    ["linked decision", decision
      ? `<code>${escapeHtml(decision.identity || "")}</code> ${escapeHtml(decision.outcome || "")}`
      : "none (no maker decision)"],
    ["freshness", fact(view.freshest)],
    ["derived confidence", fact(view.derived_confidence)],
    ["provider / model", `${providerLabel(view)} / ${fact(view.model)}`],
    ["as_of / expires_at", `${fact(view.as_of)} / ${fact(view.expires_at)}`],
    ["market state hash", fact(view.market_state_hash)],
  ]);
  const summary = section("Prediction", linkage);
  if (!horizons) {
    return summary + section("Prediction · horizons", rows([
      ["horizons", fact(view.horizons || { known: false, reason: "no prediction record yet" })],
      ["note", "无预测记录 ⇒ 不绘制概率；绝不显示中性 50% 冒充事实"],
    ]) + `<div id="${id}" class="chart-host" style="height:200px"></div>`);
  }
  const tableRows = horizons.map((item) =>
    [`${horizonLabel(item.horizon_ms)}`].concat(HORIZON_CATEGORIES.map((category) => fact({
      known: Number.isFinite(Number(item[category])), value: item[category],
      reason: "missing category" }))));
  return summary + section("Prediction · horizons (probability by category)",
    table(["horizon"].concat(HORIZON_CATEGORIES), tableRows) +
    `<div id="${id}" class="chart-host" style="height:220px"></div>`) +
    (chartsAvailable() ? "" : '<div class="muted">chart library unavailable in this environment (table above is authoritative)</div>');
}

/** 真实绘制多 horizon 概率（ECharts grouped bar）。 */
export function mountPredictionPanel(host, prediction) {
  const view = prediction || {};
  const horizons = view.horizons && view.horizons.known ? view.horizons.value : null;
  if (!host) return null;
  if (!horizons || !horizons.length) {
    host.innerHTML = '<div class="row"><span class="k">horizons</span><span class="v unknown">UNKNOWN (no prediction record)</span></div>';
    return null;
  }
  const labels = horizons.map((item) => horizonLabel(item.horizon_ms));
  const series = HORIZON_CATEGORIES.map((category) => ({
    name: category, values: horizons.map((item) => Number(item[category])),
  }));
  return mountChart(host, {
    legend: { top: 0, textStyle: { color: "#8b93a1", fontSize: 10 } },
    grid: { left: 48, right: 16, top: 30, bottom: 26 },
    xAxis: { type: "category", data: labels, axisLabel: { color: "#8b93a1", fontSize: 10 },
             axisLine: { lineStyle: { color: "#2a2f3a" } } },
    yAxis: { type: "value", max: 1, splitLine: { lineStyle: { color: "#20242e" } },
             axisLabel: { color: "#8b93a1", fontSize: 10 } },
    series: series.map((item) => ({ type: "bar", name: item.name, data: item.values, barMaxWidth: 18 })),
  });
}
