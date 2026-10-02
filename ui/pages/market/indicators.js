/**
 * Probex indicator math —— **独立于 chart renderer** 的纯函数（可在 Node 单测）。
 *
 * 库已有实现（MA/EMA/VOL 等）直接用 klinecharts 内建；这里只补 VWAP / ATR，
 * 并用 `registerProbexIndicators(klinecharts)` 注册为 klinecharts indicator。
 */

/** 简单移动平均；样本不足返回 null（不伪造）。 */
export function sma(values, period) {
  const out = new Array(values.length).fill(null);
  if (!Number.isFinite(period) || period <= 0) return out;
  let sum = 0;
  for (let i = 0; i < values.length; i += 1) {
    const value = values[i];
    const usable = Number.isFinite(value) ? value : null;
    if (usable !== null) sum += usable;
    if (i >= period) {
      const dropped = values[i - period];
      if (Number.isFinite(dropped)) sum -= dropped;
    }
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

/** 指数移动平均（seed 用第一个有效值）。 */
export function ema(values, period) {
  const out = new Array(values.length).fill(null);
  if (!Number.isFinite(period) || period <= 0) return out;
  const alpha = 2 / (period + 1);
  let previous = null;
  for (let i = 0; i < values.length; i += 1) {
    const value = Number.isFinite(values[i]) ? values[i] : null;
    if (value === null) { out[i] = previous; continue; }
    previous = previous === null ? value : value * alpha + previous * (1 - alpha);
    out[i] = previous;
  }
  return out;
}

/** 滚动 VWAP：Σ(typical×volume) / Σ(volume)；volume 全 0 时退化为 typical 均价。 */
export function vwap(candles, period) {
  const out = new Array(candles.length).fill(null);
  if (!Number.isFinite(period) || period <= 0) return out;
  for (let i = 0; i < candles.length; i += 1) {
    const start = Math.max(0, i - period + 1);
    let weighted = 0;
    let volume = 0;
    let typicalSum = 0;
    let count = 0;
    for (let j = start; j <= i; j += 1) {
      const bar = candles[j];
      const typical = (Number(bar.high) + Number(bar.low) + Number(bar.close)) / 3;
      const vol = Number.isFinite(Number(bar.volume)) ? Number(bar.volume) : 0;
      weighted += typical * vol;
      volume += vol;
      typicalSum += typical;
      count += 1;
    }
    if (count === 0) continue;
    out[i] = volume > 0 ? weighted / volume : typicalSum / count;
  }
  return out;
}

/** Wilder ATR（用前一根 close 计算真实波幅）。 */
export function atr(candles, period) {
  const out = new Array(candles.length).fill(null);
  if (!Number.isFinite(period) || period <= 0) return out;
  const trueRanges = [];
  let previousClose = null;
  for (const bar of candles) {
    const high = Number(bar.high);
    const low = Number(bar.low);
    if (!Number.isFinite(high) || !Number.isFinite(low)) { trueRanges.push(null); continue; }
    const range = previousClose === null
      ? high - low
      : Math.max(high - low, Math.abs(high - previousClose), Math.abs(low - previousClose));
    trueRanges.push(range);
    previousClose = Number(bar.close);
  }
  let value = null;
  let seed = 0;
  let seedCount = 0;
  for (let i = 0; i < trueRanges.length; i += 1) {
    const range = trueRanges[i];
    if (range === null) { out[i] = value; continue; }
    if (value === null) {
      seed += range;
      seedCount += 1;
      if (seedCount === period) { value = seed / period; out[i] = value; }
      continue;
    }
    value = (value * (period - 1) + range) / period;
    out[i] = value;
  }
  return out;
}

function seriesOf(candles, values, key) {
  return candles.map((_, index) => ({ [key]: values[index] }));
}

/** 注册 Probex 自定义指标（VWAP / ATR）。库已有 MA/EMA/VOL 不重复实现。 */
export function registerProbexIndicators(klinecharts) {
  if (!klinecharts || typeof klinecharts.registerIndicator !== "function") return false;
  klinecharts.registerIndicator({
    name: "VWAP",
    shortName: "VWAP",
    series: "price",
    precision: 2,
    calcParams: [30],
    figures: [{ key: "vwap", title: "VWAP: ", type: "line" }],
    calc: (dataList, indicator) => {
      const period = Number(indicator.calcParams[0]) || 30;
      return seriesOf(dataList, vwap(dataList, period), "vwap");
    },
  });
  klinecharts.registerIndicator({
    name: "ATR",
    shortName: "ATR",
    series: "normal",
    precision: 2,
    calcParams: [14],
    figures: [{ key: "atr", title: "ATR: ", type: "line" }],
    calc: (dataList, indicator) => {
      const period = Number(indicator.calcParams[0]) || 14;
      return seriesOf(dataList, atr(dataList, period), "atr");
    },
  });
  return true;
}

/** 页面工具栏可选的指标集合（overlay / pane 两栏）。 */
export const PROBEX_INDICATORS = [
  { name: "MA", pane: "overlay" },
  { name: "EMA", pane: "overlay" },
  { name: "VWAP", pane: "overlay" },
  { name: "VOL", pane: "pane" },
  { name: "ATR", pane: "pane" },
];
