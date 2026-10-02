/** Assistant context：URL hash（Surface + 选中对象）+ 图表选中上下文；不复制领域状态。 */
import { getSelection } from "/ui/client/selection.js";

export function currentSelection(hash = typeof window !== "undefined" ? window.location.hash : "") {
  const parts = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean);
  const surface = parts[0] || "monitor";
  const selection = {};
  for (let index = 1; index < parts.length - 1; index += 2) {
    const key = parts[index];
    if (["run", "decision", "order", "fill"].includes(key)) selection[key] = parts[index + 1];
  }
  // closure slice：chart workbench 的选中（timestamp / timeframe / candle / drawing）
  const chart = getSelection ? getSelection() : {};
  const merged = Object.assign({}, selection);
  if (chart.timestamp) merged.timestamp = String(chart.timestamp);
  if (chart.timeframe) merged.timeframe = String(chart.timeframe);
  if (chart.candle) merged.candle = typeof chart.candle === "string" ? chart.candle : JSON.stringify(chart.candle);
  if (chart.drawing) merged.drawing = String(chart.drawing);
  if (chart.run) merged.run = String(chart.run);
  return { surface: chart.surface || surface, selection: merged };
}
