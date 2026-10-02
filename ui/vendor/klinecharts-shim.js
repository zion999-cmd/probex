/**
 * klinecharts ESM shim（Probex adapter，不是三方源码）。
 *
 * klinecharts 的 `dist/index.esm.js` 在浏览器里会引用 Node 的 `process`，无法直接作为 ES module 加载；
 * 官方浏览器构建是 UMD（`dist/umd/klinecharts.min.js`，挂在 `globalThis.klinecharts`）。
 *
 * 本文件把 UMD 全局桥接成 ES module 命名导出，供：
 * - `@klinecharts/extension`（它 `import { utils } from "klinecharts"`）
 * - Probex workbench
 * 通过 import map 使用。**不修改任何三方源码**；UMD 未加载时导出 undefined（调用方据此降级）。
 */
const kc = typeof globalThis !== "undefined" ? globalThis.klinecharts : undefined;

function fn(name) {
  if (!kc || typeof kc[name] !== "function") return undefined;
  return (...args) => kc[name](...args);
}

export const init = fn("init");
export const dispose = fn("dispose");
export const registerOverlay = fn("registerOverlay");
export const registerIndicator = fn("registerIndicator");
export const registerFigure = fn("registerFigure");
export const registerLocale = fn("registerLocale");
export const getSupportedIndicators = fn("getSupportedIndicators");
export const getSupportedOverlays = fn("getSupportedOverlays");
export const utils = kc ? kc.utils : undefined;
export const version = kc ? kc.version : undefined;

export default kc;
