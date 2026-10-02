/** System 轻量图表（ECharts）：latency / rate-limit / anomaly。事实/配置仍以表格为主。 */
import { barOption, mountChart } from "/ui/client/charts.js";

export const OPS_CHART_HOSTS = { latency: "ops-latency", rate: "ops-rate", anomalies: "ops-anomalies" };

export function opsChartsSection() {
  return `<div id="${OPS_CHART_HOSTS.latency}" class="chart-host" style="height:190px"></div>` +
    `<div id="${OPS_CHART_HOSTS.rate}" class="chart-host" style="height:160px"></div>` +
    `<div id="${OPS_CHART_HOSTS.anomalies}" class="chart-host" style="height:160px"></div>` +
    '<div class="muted">charts are read-only visualizations; tables below remain authoritative</div>';
}

export function mountOpsCharts({ latency, rateLimits, anomalies }) {
  const stages = (latency && !latency.unavailable && latency.latency && latency.latency.stages) || [];
  const samples = stages.filter((stage) => stage.sample_count > 0);
  if (samples.length) {
    mountChart(document.getElementById(OPS_CHART_HOSTS.latency), barOption({
      categories: samples.map((stage) => stage.stage),
      values: samples.map((stage) => (stage.p50 && stage.p50.known ? stage.p50.value : null)),
      name: "p50 ms", unit: " ms",
    }));
  } else {
    const host = document.getElementById(OPS_CHART_HOSTS.latency);
    if (host) host.innerHTML = '<span class="unknown">UNKNOWN (no latency samples)</span>';
  }
  const governor = rateLimits && !rateLimits.unavailable ? rateLimits.governor : null;
  if (governor) {
    mountChart(document.getElementById(OPS_CHART_HOSTS.rate), barOption({
      categories: ["request remaining", "order remaining"],
      values: [
        governor.request && governor.request.remaining && governor.request.remaining.known
          ? governor.request.remaining.value : null,
        governor.order && governor.order.remaining && governor.order.remaining.known
          ? governor.order.remaining.value : null,
      ],
      name: "remaining",
    }));
  } else {
    const host = document.getElementById(OPS_CHART_HOSTS.rate);
    if (host) host.innerHTML = '<span class="unknown">UNKNOWN (no rate-limit facts)</span>';
  }
  const blockers = (anomalies && !anomalies.unavailable && anomalies.blockers) || [];
  const bySeverity = { BLOCKING: 0, DEGRADED: 0, INFO: 0 };
  for (const blocker of blockers) {
    bySeverity[blocker.severity] = (bySeverity[blocker.severity] || 0) + 1;
  }
  if (blockers.length) {
    mountChart(document.getElementById(OPS_CHART_HOSTS.anomalies), barOption({
      categories: Object.keys(bySeverity), values: Object.values(bySeverity), name: "execution anomalies",
    }));
  } else {
    const host = document.getElementById(OPS_CHART_HOSTS.anomalies);
    if (host) host.innerHTML = '<span class="ok known">0 execution anomalies</span>';
  }
}
