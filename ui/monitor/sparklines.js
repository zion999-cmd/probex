/** Monitor sparklines（ECharts）：equity / PnL / exposure 的少量趋势，不做几十张 debug 表。 */
import { mountChart } from "/ui/client/charts.js";

export const SPARK_HOSTS = ["spark-equity", "spark-pnl", "spark-exposure"];

// P0018：图表实例按 host 复用（refresh 只 setOption），host 节点身份保持稳定。
const chartInstances = new Map();

export function sparklinesSection() {
  return `<div class="spark-row">` +
    `<div class="spark"><div class="spark-title">equity</div><div id="${SPARK_HOSTS[0]}" class="spark-host"></div></div>` +
    `<div class="spark"><div class="spark-title">unrealized PnL</div><div id="${SPARK_HOSTS[1]}" class="spark-host"></div></div>` +
    `<div class="spark"><div class="spark-title">exposure</div><div id="${SPARK_HOSTS[2]}" class="spark-host"></div></div>` +
    `</div><div class="muted" id="spark-note"></div>`;
}

function mini(hostId, values, color) {
  const host = document.getElementById(hostId);
  if (!host) return null;
  const option = {
    grid: { left: 4, right: 4, top: 6, bottom: 4 },
    xAxis: { type: "category", show: false, data: values.map((_, index) => index) },
    yAxis: { type: "value", show: false, scale: true },
    tooltip: { trigger: "axis" },
    series: [{ type: "line", data: values, showSymbol: false, lineStyle: { width: 1.5, color },
               areaStyle: { color, opacity: 0.12 } }],
  };
  const existing = chartInstances.get(hostId);
  if (existing) {
    existing.setOption(option);
    return existing;
  }
  const chart = mountChart(host, option);
  if (chart) chartInstances.set(hostId, chart);
  return chart;
}

function markUnknown(hostId, text) {
  const host = document.getElementById(hostId);
  if (!host) return;
  if (!host.querySelector(".unknown")) host.innerHTML = "";
  host.textContent = text;
  host.classList.add("unknown");
}

export function mountSparklines(timeline) {
  const view = timeline && !timeline.unavailable ? timeline.timeline : null;
  const points = (view && view.points) || [];
  const note = document.getElementById("spark-note");
  if (!points.length) {
    for (const id of SPARK_HOSTS) markUnknown(id, "UNKNOWN");
    if (note) note.textContent = timeline && timeline.unavailable
      ? `UNKNOWN (${timeline.unavailable})` : "UNKNOWN (no recorded account samples yet)";
    return;
  }
  const value = (point, key) => (point[key] && point[key].known ? Number(point[key].value) : null);
  mini(SPARK_HOSTS[0], points.map((point) => value(point, "equity")), "#7aa2f7");
  mini(SPARK_HOSTS[1],
       points.map((point) => (value(point, "unrealized_pnl") !== null ? value(point, "unrealized_pnl") : null)),
       "#7ec699");
  mini(SPARK_HOSTS[2], points.map((point) => value(point, "exposure_total")), "#e0af68");
  if (note) note.textContent = `${points.length} recorded samples`;
}
