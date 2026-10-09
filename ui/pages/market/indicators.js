/**
 * 指标能力（P0001.17 §5）：
 *
 * - **MA / EMA / VOL**：由成熟开源图表库（klinecharts）计算与绘制（人类裁决允许的"基础指标"）；
 * - **VWAP / ATR**：后端**没有**这两个事实（`FeatureEngine` 不消费 TRADE 事件 ⇒ `trade.vwap` 不可用；
 *   且不存在 ATR 实现）⇒ 按裁决"不要在 UI 重新计算业务指标"，**不在 UI 自算**，由页面以
 *   `NOT_AVAILABLE` + reason 呈现（见 `market/indicators.js` 的 `UNAVAILABLE_INDICATORS`）。
 */
export function registerProbexIndicators() {
  return false;                       // 不注册任何自算指标（避免 UI 侧业务计算）
}

//: 后端无事实的指标（页面必须显示真实原因，不得自算或留空）
export const UNAVAILABLE_INDICATORS = [
  { name: "VWAP", reason: "后端无 VWAP 事实：FeatureEngine 不消费 TRADE 事件（trade.vwap = UNAVAILABLE）" },
  { name: "ATR", reason: "后端无 ATR 实现（本阶段不存在该事实）" },
];

/** 页面工具栏可选的指标集合（overlay / pane 两栏）。 */
export const PROBEX_INDICATORS = [
  { name: "MA", pane: "overlay" },
  { name: "EMA", pane: "overlay" },
  { name: "VOL", pane: "pane" },
];
