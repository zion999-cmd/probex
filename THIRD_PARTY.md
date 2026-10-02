# Third-party UI dependencies (chart workbench)

本阶段（图表/UI 产品化）**只**引入以下前端库；均由 npm 官方包提供，通过 `/vendor/<pkg>/...`
从 `node_modules` **原样**提供（无 build step、不复制源码、不打补丁、不 fork）。
`package.json` + `package-lock.json` 已入库；`node_modules/` 不入库（运行前 `npm install`）。

| package | version | license | 使用的 dist | 用途 |
| --- | --- | --- | --- | --- |
| `klinecharts` | 10.0.3 | Apache-2.0 | `dist/umd/klinecharts.min.js`（浏览器 UMD）、`dist/index.esm.js`（**未加载**，Node 侧引用 `process`） | candlestick / zoom / pan / crosshair / tooltip / axis / last price / 内建指标（MA/EMA/VOL/…）/ overlay 框架 |
| `@klinecharts/extension` | 0.1.0 | Apache-2.0 | `dist/index.js` + `dist/overlays/*` | 复用 drawing overlays：`rect` / `arrow` / `measure` / `fibonacciSegment` / `fibonacciExtension` |
| `echarts` | 6.1.0 | Apache-2.0 | `dist/echarts.esm.min.mjs` | Prediction 多 horizon、Performance（equity/drawdown/exposure/metrics/compare）、Monitor sparklines、System 轻量图 |

**未使用**：`@klinecharts/pro` 0.1.1（会额外引入 `lodash` + `solid-js`；其开箱 UI 与 Probex 的
semantic overlays / replay 同步 / canonical navigation 冲突，且本阶段只需薄工具栏）。

**适配层（Probex 自写，不是三方源码）**：`ui/vendor/klinecharts-shim.js` 把 klinecharts 的 UMD 全局
桥接成 ES module 命名导出（`init` / `registerOverlay` / `registerIndicator` / `utils` …），
供 `@klinecharts/extension`（它 `import { utils } from "klinecharts"`）与 Probex workbench 通过
`importmap` 使用。

**Probex 自己实现的部分（不含任何图表基础轮子）**：candle 数据适配（后端 `product/candles.py`）、
VWAP/ATR 指标数学（`ui/pages/market/indicators.js`，renderer-independent）、semantic overlays 的内容与
canonical identity 导航、replay/时间同步、Assistant 图表上下文。

Reproduce: `npm install` → `python3 -m runtime.assembly ...`（静态服务按白名单提供 `/vendor/`）。
