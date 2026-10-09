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
  { name: "MA", pane: "overlay" },
  { name: "EMA", pane: "overlay" },
  { name: "VOL", pane: "pane" },
];
