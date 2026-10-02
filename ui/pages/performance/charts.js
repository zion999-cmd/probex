/**
 * Performance 图表（Apache ECharts）：equity / drawdown / exposure / run metrics / compare。
 *
 * 只画**已记录事实**的序列与已报告的 Metric Contract 值；不重新计算 metric。
 * drawdown 曲线是对 equity 序列的**可视化**，合同口径仍是报告里的 `run_mdd`。
 */
import { escapeHtml, fact, rows, section, table } from "/ui/client/render.js";
import { barOption, chartsAvailable, lineOption, mountChart } from "/ui/client/charts.js";

const HOSTS = {
  equity: "perf-equity", drawdown: "perf-drawdown", exposure: "perf-exposure",
  metrics: "perf-metrics", pnl: "perf-pnl", compare: "perf-compare",
};

function points(timeline) {
  const view = timeline && !timeline.unavailable ? timeline.timeline : null;
  return (view && view.points) || [];
}

export function performanceChartsSection({ timeline, summary, comparison, selectedRun }) {
  const series = points(timeline);
  const timelineBlock = series.length
    ? `<div id="${HOSTS.equity}" class="chart-host" style="height:240px"></div>` +
      `<div id="${HOSTS.drawdown}" class="chart-host" style="height:170px"></div>` +
      `<div id="${HOSTS.exposure}" class="chart-host" style="height:170px"></div>`
    : rows([["equity / exposure series", timeline && timeline.unavailable
        ? `<span class="unknown">UNKNOWN (${escapeHtml(timeline.unavailable)})</span>`
        : '<span class="unknown">UNKNOWN (no recorded samples for this run)</span>'],
        ["note", "历史 run 的 equity 序列未持久化 ⇒ 只有当前/已接线 run 有曲线"]]);
  const metricValues = (summary && summary.run_summary && summary.run_summary.metrics) || {};
  const numeric = Object.entries(metricValues).filter(([, value]) => value && value.known
    && typeof value.value === "number");
  const metricBlock = numeric.length
    ? `<div id="${HOSTS.metrics}" class="chart-host" style="height:220px"></div>` +
      table(["metric", "value"], numeric.map(([name, value]) => [escapeHtml(name), fact(value)]))
    : rows([["run metrics", '<span class="unknown">UNKNOWN (no known numeric metrics for this run)</span>']]);
  const compareRows = comparison && comparison.comparison
    ? (comparison.comparison.metrics || []).map((item) => [escapeHtml(item.name), fact(item.left),
        fact(item.right), fact(item.delta)])
    : [];
  const compareBlock = compareRows.length
    ? table(["metric", "left", "right", "delta"], compareRows) +
      `<div id="${HOSTS.compare}" class="chart-host" style="height:220px"></div>`
    : rows([["compare", "requires two runs"]]);
  return section(`Equity / exposure timeline (G3)${selectedRun ? ` · ${escapeHtml(selectedRun)}` : ""}`,
                 timelineBlock) +
    section("Run metrics (Metric Contract)", metricBlock) +
    section("Compare runs", compareBlock) +
    (chartsAvailable() ? "" : '<div class="muted">chart library unavailable in this environment</div>');
}

export function mountPerformanceCharts({ timeline, summary, comparison }) {
  const series = points(timeline);
  if (series.length) {
    const stamps = series.map((point) => String(point.ts));
    mountChart(document.getElementById(HOSTS.equity), lineOption({
      timestamps: stamps,
      series: [{ name: "equity", values: series.map((point) => (point.equity && point.equity.known
        ? point.equity.value : null)) }],
    }));
    // drawdown 曲线：对 equity 序列的可视化（合同口径 = run_mdd）
    let peak = null;
    const underwater = series.map((point) => {
      const equity = point.equity && point.equity.known ? Number(point.equity.value) : null;
      if (equity === null) return null;
      peak = peak === null ? equity : Math.max(peak, equity);
      return peak > 0 ? Number((((equity - peak) / peak) * 100).toFixed(4)) : null;
    });
    mountChart(document.getElementById(HOSTS.drawdown), lineOption({
      timestamps: stamps, series: [{ name: "drawdown % of peak (display)", values: underwater }],
    }));
    mountChart(document.getElementById(HOSTS.exposure), lineOption({
      timestamps: stamps,
      series: [
        { name: "exposure total", values: series.map((point) => (point.exposure_total && point.exposure_total.known
          ? point.exposure_total.value : null)) },
        { name: "exposure confirmed", values: series.map((point) => (point.exposure_confirmed && point.exposure_confirmed.known
          ? point.exposure_confirmed.value : null)) },
      ],
    }));
  }
  const metricValues = (summary && summary.run_summary && summary.run_summary.metrics) || {};
  const numeric = Object.entries(metricValues).filter(([, value]) => value && value.known
    && typeof value.value === "number");
  if (numeric.length) {
    mountChart(document.getElementById(HOSTS.metrics), barOption({
      categories: numeric.map(([name]) => name), values: numeric.map(([, value]) => value.value),
      name: "value",
    }));
  }
  const comparisonRows = (comparison && comparison.comparison && comparison.comparison.metrics) || [];
  const known = comparisonRows.filter((item) => (item.left && item.left.known)
    || (item.right && item.right.known));
  if (known.length) {
    mountChart(document.getElementById(HOSTS.compare), {
      legend: { top: 0, textStyle: { color: "#8b93a1", fontSize: 10 } },
      grid: { left: 56, right: 16, top: 28, bottom: 28 },
      xAxis: { type: "category", data: known.map((item) => item.name),
               axisLabel: { color: "#8b93a1", fontSize: 10 }, axisLine: { lineStyle: { color: "#2a2f3a" } } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: "#20242e" } },
               axisLabel: { color: "#8b93a1", fontSize: 10 } },
      series: [
        { type: "bar", name: "left", data: known.map((item) => (item.left && item.left.known ? item.left.value : null)) },
        { type: "bar", name: "right", data: known.map((item) => (item.right && item.right.known ? item.right.value : null)) },
      ],
    });
  }
}
