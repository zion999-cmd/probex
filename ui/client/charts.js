/**
 * ECharts mount helper（统一 dark 主题 + resize）。无 echarts（例如 Node 渲染守卫）时安全降级。
 */
import * as echarts from "/vendor/echarts/dist/echarts.esm.min.mjs";

export const CHART_COLORS = ["#7aa2f7", "#7ec699", "#e0af68", "#e06c75", "#bb9af7", "#2ac3de", "#73daca"];

export function chartsAvailable() {
  return typeof echarts !== "undefined" && !!echarts && typeof echarts.init === "function";
}

export const DARK_AXIS = {
  axisLine: { lineStyle: { color: "#2a2f3a" } },
  axisLabel: { color: "#8b93a1", fontSize: 10 },
  splitLine: { lineStyle: { color: "#20242e" } },
};

export function darkBase(option) {
  return Object.assign({
    backgroundColor: "transparent",
    textStyle: { color: "#d7dae0", fontSize: 11 },
    grid: { left: 48, right: 16, top: 28, bottom: 28 },
    tooltip: { trigger: "axis", backgroundColor: "#161a23", borderColor: "#2a2f3a",
               textStyle: { color: "#d7dae0", fontSize: 11 } },
    color: CHART_COLORS,
    animation: false,
  }, option);
}

/** 在 host 元素上挂载一个 ECharts 图；不可用/无 host ⇒ null（调用方渲染 UNKNOWN 说明）。 */
export function mountChart(host, option) {
  if (!chartsAvailable() || !host) return null;
  const chart = echarts.init(host, null, { renderer: "canvas" });
  chart.setOption(darkBase(option));
  const onResize = () => chart.resize();
  window.addEventListener("resize", onResize);
  chart.__probexDispose = () => { window.removeEventListener("resize", onResize); chart.dispose(); };
  return chart;
}

export function barOption({ categories, values, name = "value", unit = "" }) {
  return {
    grid: { left: 56, right: 16, top: 24, bottom: 28 },
    xAxis: Object.assign({ type: "category", data: categories }, DARK_AXIS),
    yAxis: Object.assign({ type: "value" }, DARK_AXIS),
    series: [{ type: "bar", name, data: values, barMaxWidth: 26,
               label: { show: true, position: "top", color: "#8b93a1", fontSize: 10,
                        formatter: (params) => (params.value === null ? "" : `${params.value}${unit}`) } }],
  };
}

export function lineOption({ timestamps, series, unit = "" }) {
  return {
    grid: { left: 56, right: 16, top: 28, bottom: 28 },
    xAxis: Object.assign({ type: "category", data: timestamps,
                           axisLabel: { color: "#8b93a1", fontSize: 10 } }, DARK_AXIS),
    yAxis: Object.assign({ type: "value" }, DARK_AXIS),
    series: (series || []).map((item, index) => ({
      type: "line", name: item.name, data: item.values, showSymbol: false, smooth: false,
      connectNulls: false, lineStyle: { width: 1.5, color: CHART_COLORS[index % CHART_COLORS.length] },
      itemStyle: { color: CHART_COLORS[index % CHART_COLORS.length] },
    })),
  };
}
