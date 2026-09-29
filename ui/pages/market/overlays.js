/** Trades / decisions / execution overlays（全部来自服务端事实）。 */
import { escapeHtml, fact, rows, table } from "/ui/client/render.js";

export function tradesTable(trades) {
  const prints = (trades && trades.prints) || [];
  return table(["ts", "aggressor", "price", "quantity"],
    prints.slice(-20).map((print) => [String(print.ts), escapeHtml(print.aggressor),
      String(print.price), String(print.quantity)]));
}

export function decisionTable(overlays) {
  const decisions = (overlays && overlays.decisions) || [];
  return table(["ts", "side", "action", "price", "qty", "decision", "reason"],
    decisions.slice(-20).map((item) => [String(item.ts), escapeHtml(item.side), escapeHtml(item.action),
      fact(item.price), fact(item.quantity), fact(item.decision_id), fact(item.reason)]));
}

export function executionTable(overlays) {
  const executions = (overlays && overlays.executions) || [];
  return table(["ts", "order", "event", "detail"],
    executions.slice(-20).map((item) => [String(item.ts), escapeHtml(item.client_order_id),
      escapeHtml(item.event), escapeHtml(item.detail || "")]));
}

export function boundsNote(payload) {
  const bounds = (payload && payload.bounds) || {};
  return rows([
    ["window_ms", String(bounds.window_ms ?? "UNKNOWN")],
    ["bucket_ms", String(bounds.bucket_ms ?? "UNKNOWN")],
    ["max_points", String(bounds.max_points ?? "UNKNOWN")],
    ["price_levels", String(bounds.price_levels ?? "UNKNOWN")],
    ["truncated", String(Boolean(payload && payload.truncated))],
  ]);
}
