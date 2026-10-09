/**
 * 指标能力（P0001.17 §5）：
 *
 * - **MA / EMA / VOL**：由成熟开源图表库（klinecharts）计算与绘制（人类裁决允许的"基础指标"）；
 * - **VWAP / ATR**：后端**没有**这两个事实（`FeatureEngine` 不消费 TRADE 事件 ⇒ `trade.vwap` 不可用；
 *   且不存在 ATR 实现）⇒ 按裁决"不要在 UI 重新计算业务指标"，**不在 UI 自算**，由页面以
 *   `NOT_AVAILABLE` + reason 呈现（见 `market/indicators.js` 的 `UNAVAILABLE_INDICATORS`）。
 */
/**
 * 注册 **后端事实** 驱动的指标 overlay：数据由 `/api/v1/market/candles` 返回
 * （candle.vwap = 真实成交均价；indicators.atr = 服务端 Wilder ATR）。本函数只做**对齐与绘制**，
 * 不做任何指标计算（§5）。
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
