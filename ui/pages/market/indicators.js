/**
 * 指标能力（P0001.17 §5）：
 *
 * - **MA / EMA / VOL**：由成熟开源图表库（klinecharts）计算与绘制（人类裁决允许的"基础指标"）；
 * - **VWAP / ATR**：后端事实（candle.vwap = 真实成交均价；indicators.atr = 服务端 Wilder ATR）。
 *   UI 只做对齐与绘制（`overlayPointsFromBackend` / `atrPointsFromBackend`），**不做任何指标计算**。
 */
export function registerProbexIndicators() {
  return false;                       // 不注册自算指标；后端事实通过 createOverlay 直接绘制
}

/** 把后端指标事实按 candle 时间对齐成 overlay 数据点（仅映射，不计算）。 */
export function overlayPointsFromBackend(candles, key) {
  return (candles || []).filter((c) => c[key] !== null && c[key] !== undefined)
    .map((c) => ({ timestamp: c.ts, value: c[key] }));
}

/** 后端 ATR 序列 → overlay 数据点（`indicators.atr.points`）。 */
export function atrPointsFromBackend(atrPoints) {
  return (atrPoints || []).filter((p) => p.atr !== null && p.atr !== undefined)
    .map((p) => ({ timestamp: p.ts, value: p.atr }));
}

//: 后端事实指标（页面从 API 读取；缺失时显示 NOT_AVAILABLE + 原因，绝不在 UI 自算）
export const BACKEND_FACT_INDICATORS = [
  { name: "VWAP", source: "candles[].vwap（真实成交均价，服务端聚合）" },
  { name: "ATR", source: "indicators.atr.points（服务端 Wilder ATR, period=14）" },
];

/** 页面工具栏可选的指标集合（overlay / pane 两栏）。 */
export const PROBEX_INDICATORS = [
  { name: "MA", pane: "overlay", calcParams: [5, 10, 30], label: "周期" },
  { name: "EMA", pane: "overlay", calcParams: [12, 26], label: "周期" },
  { name: "VOL", pane: "pane", calcParams: [5, 10], label: "周期" },
];

/** 指标参数校验：只接受 1–400 的正整数、最多 6 个（防非法输入进入图表库）。 */
export function parseCalcParams(text) {
  const parts = String(text || "").split(",").map((part) => part.trim()).filter(Boolean);
  if (!parts.length || parts.length > 6) return null;
  const values = [];
  for (const part of parts) {
    if (!/^[0-9]{1,3}$/.test(part)) return null;
    const value = Number(part);
    if (!Number.isInteger(value) || value < 1 || value > 400) return null;
    values.push(value);
  }
  return values;
}
