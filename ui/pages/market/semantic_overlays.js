/**
 * Probex semantic overlays on the K-line chart.
 *
 * 事实来源：F-08 的 `evidence.trace`（按时间排序、带 canonical identity）；
 * 本模块只做**展示映射 + 导航**，不复制/改写任何业务事实。
 */

/** trace stage → 展示样式 + 导航实体类型（kind ⇒ navigation.entityHash）。 */
export const SEMANTIC_STAGES = {
  prediction: { label: "prediction", color: "#7aa2f7", kind: "prediction" },
  maker_decision: { label: "decision", color: "#bb9af7", kind: "decision" },
  risk: { label: "risk", color: "#e0af68", kind: "order" },
  readiness: { label: "readiness", color: "#9aa4b2", kind: null },
  normalization: { label: "normalization", color: "#7dcfff", kind: "order" },
  order: { label: "order", color: "#7ec699", kind: "order" },
  ack: { label: "ack", color: "#2ac3de", kind: "order" },
  execution_event: { label: "event", color: "#9aa4b2", kind: "execution_event" },
  cancel: { label: "cancel", color: "#e06c75", kind: "order" },
  fill: { label: "fill", color: "#73daca", kind: "fill" },
  unknown: { label: "UNKNOWN", color: "#d8a657", kind: null },
  reconciliation: { label: "reconciliation", color: "#c0caf5", kind: null },
};

export const SEMANTIC_OVERLAY_NAME = "probexSemantic";

/** 注册后端事实指标 overlay（VWAP / ATR 线；**不计算**指标，只绘制 API 返回值）。 */
export function registerFactIndicators(klinecharts) {
  if (!klinecharts || typeof klinecharts.registerOverlay !== "function") return false;
  const lineTemplate = (name, title) => ({
    name, totalStep: 0, lock: true,
    createPointFigures: ({ coordinates, overlay }) => {
      if (!coordinates || coordinates.length === 0) return [];
      return [{
        type: "line", attrs: { coordinates },
        styles: { style: "solid", size: 1, color: name === "probexVwap" ? "#f5a524" : "#7aa2f7" },
      }, {
        type: "circle", attrs: { x: coordinates[coordinates.length - 1].x,
                                 y: coordinates[coordinates.length - 1].y, r: 2 },
        styles: { style: "fill", color: name === "probexVwap" ? "#f5a524" : "#7aa2f7" },
        ignoreEvent: true,
      }];
    },
  });
  klinecharts.registerOverlay(lineTemplate("probexVwap", "VWAP(backend)"));
  klinecharts.registerOverlay(lineTemplate("probexAtr", "ATR(backend)"));
  return true;
}

/** 注册 semantic overlay（marker = circle + text；点击跳转 canonical 实体）。 */
export function registerProbexOverlays(klinecharts) {
  if (!klinecharts || typeof klinecharts.registerOverlay !== "function") return false;
  klinecharts.registerOverlay({
    name: SEMANTIC_OVERLAY_NAME,
    totalStep: 2,
    lock: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, overlay }) => {
      const point = coordinates && coordinates[0];
      if (!point) return [];
      const data = (overlay && overlay.extendData) || {};
      const color = data.color || "#9aa4b2";
      return [
        { type: "circle", attrs: { x: point.x, y: point.y, r: 3.5 },
          styles: { style: "fill", color } },
        { type: "text", attrs: { x: point.x, y: point.y - 10, text: ` ${data.label || ""} `,
                                 align: "center", baseline: "bottom" },
          styles: { style: "fill", color, size: 10, backgroundColor: "rgba(15,17,23,0.78)" } },
      ];
    },
    onClick: ({ overlay }) => {
      const data = (overlay && overlay.extendData) || {};
      if (data.href && typeof window !== "undefined" && window.location) {
        window.location.hash = String(data.href).replace(/^#/, "");
      }
    },
  });
  return true;
}

/** evidence trace → semantic overlay 列表；`priceAt(ts)` 提供该时刻的价格（无则跳过）。 */
export function semanticOverlays(trace, priceAt, entityHashFn) {
  const overlays = [];
  for (const entry of trace || []) {
    const stage = SEMANTIC_STAGES[entry && entry.stage];
    if (!stage) continue;
    if (!entry.ts || !entry.ts.known) continue;
    const timestamp = Number(entry.ts.value);
    const value = priceAt(timestamp);
    if (value === null || value === undefined) continue;
    const identity = entry.identity && entry.identity.known ? String(entry.identity.value) : null;
    let href = null;
    if (stage.kind && identity && identity !== "unknown") {
      try { href = entityHashFn(stage.kind, identity); } catch (error) { href = null; }
    }
    overlays.push({
      name: SEMANTIC_OVERLAY_NAME,
      lock: true,
      points: [{ timestamp, value }],
      extendData: { label: stage.label, color: stage.color, href, stage: entry.stage,
                    outcome: entry.outcome, identity },
    });
  }
  return overlays;
}
