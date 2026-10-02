/**
 * 测试资产：在 Node 中真实执行 Probex 页面 render()（无浏览器 DOM）。
 *
 * 用途：证明 F3（ops 不再 [object Object]）与 F7（缺阶段 ABSENT 文案）等
 * **渲染层**行为——这些是字符串静态测试无法覆盖的。
 *
 * 用法：node render_page.mjs <base-url> <ui/pages/xxx/page.js> <restJson>
 * 输出：一行 JSON { ok, bytes, sections, html } （失败时 { ok:false, error }）
 */
import fs from "node:fs";
import path from "node:path";

const [, , base, modulePath, restJson = "[]"] = process.argv;
const repo = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../..");

const BASE_DEPS = ["ui/client/render.js", "ui/client/api.js", "ui/app/surfaces.js",
                   "ui/app/navigation.js"];
const EXTRA_DEPS = {
  "ui/pages/market/page.js": ["ui/pages/market/heatmap.js", "ui/pages/market/overlays.js",
                              "ui/pages/market/replay.js", "ui/pages/market/timeline.js"],
};

function strip(source) {
  let out = source
    .replace(/import\s*\{[\s\S]*?\}\s*from\s*"[^"]*";/g, "")
    .replace(/^\s*import\s+[^\n]*$/gm, "");
  out = out.replace(/\bexport\s+(async\s+function|function|const|let|var|class)\b/g, "$1");
  out = out.replace(/^\s*export\s*\{[^}]*\};?\s*$/gm, "");
  return out;
}

const noop = () => {};
const ctx2d = new Proxy({}, { get: () => noop });
function fakeElement() {
  const base = { width: 600, height: 400, style: {}, dataset: {},
    classList: { add: noop, remove: noop, toggle: noop }, getContext: () => ctx2d,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 600, height: 400 }),
    querySelectorAll: () => [], querySelector: () => fakeElement(),
    appendChild: noop, addEventListener: noop, setAttribute: noop, removeAttribute: noop };
  return new Proxy(base, { get: (target, prop) => (prop in target ? target[prop] : noop) });
}
globalThis.document = { getElementById: () => fakeElement(), querySelector: () => fakeElement(),
  querySelectorAll: () => [], createElement: () => fakeElement(), addEventListener: noop };
globalThis.window = { addEventListener: noop, location: { hash: "" }, devicePixelRatio: 1 };
globalThis.requestAnimationFrame = (cb) => { cb(0); return 1; };
globalThis.cancelAnimationFrame = noop;

const realFetch = globalThis.fetch;
globalThis.fetch = (url, options) => realFetch(
  typeof url === "string" && url.startsWith("/") ? base + url : url, options);

const files = [...BASE_DEPS, ...(EXTRA_DEPS[modulePath] || []), modulePath];
const body = files.map((file) => strip(fs.readFileSync(path.join(repo, file), "utf8")))
  .join("\n;\n") + "\nreturn render;";

try {
  const render = new Function(body)();
  const html = await render(JSON.parse(restJson));
  console.log(JSON.stringify({ ok: true, bytes: html.length, html,
    sections: [...html.matchAll(/<h2>(.*?)<\/h2>/g)].map((m) => m[1]) }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: String(error) }));
  process.exit(0);
}
