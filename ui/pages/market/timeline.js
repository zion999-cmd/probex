/** 特征面板：只显示服务端 projection 的值（浏览器不重算市场事实）。 */
import { fact, rows } from "/ui/client/render.js";

export function featurePanels(timeline) {
  const points = (timeline && timeline.points) || [];
  const last = points[points.length - 1];
  if (!last) return rows([["timeline", '<span class="unknown">UNKNOWN (no points)</span>']]);
  return rows([
    ["ts", String(last.ts)],
    ["spread", fact(last.spread)],
    ["microprice", fact(last.microprice)],
    ["imbalance (l1)", fact(last.imbalance)],
    ["OFI (1s)", fact(last.ofi)],
    ["best bid", fact(last.best_bid)],
    ["best ask", fact(last.best_ask)],
    ["mid", fact(last.mid)],
  ]);
}

export function healthStrip(health) {
  const segments = (health && health.segments) || [];
  if (!segments.length) return '<span class="unknown">UNKNOWN (no health transitions)</span>';
  return segments.map((segment) => {
    const cls = segment.health === "healthy" ? "known" : "bad";
    return `<span class="${cls}" title="${segment.reason}">${segment.health}</span>`;
  }).join(" → ");
}
