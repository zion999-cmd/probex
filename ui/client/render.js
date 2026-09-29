/** 渲染原语：UNKNOWN 一律显式（P0001.10.2 SC-3）。 */
export function fact(value) {
  if (value === null || value === undefined) return '<span class="unknown">UNKNOWN</span>';
  if (typeof value !== "object" || !("known" in value)) return `<span class="known">${escapeHtml(String(value))}</span>`;
  if (!value.known) return `<span class="unknown">UNKNOWN (${escapeHtml(value.reason || "unknown")})</span>`;
  const v = value.value;
  return `<span class="known">${v === null ? "null" : escapeHtml(String(v))}</span>`;
}

export function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function rows(pairs) {
  return pairs.map(([k, v]) => `<div class="row"><span class="k">${escapeHtml(k)}</span><span class="v">${v}</span></div>`).join("");
}

export function section(title, body) {
  return `<section><h2>${escapeHtml(title)}</h2>${body}</section>`;
}

export function factRows(object) {
  return rows(Object.entries(object || {}).map(([k, v]) => [k, fact(v)]));
}

export function table(headers, rowsData) {
  if (!rowsData.length) return '<div class="row"><span class="k">rows</span><span class="v known">0</span></div>';
  return `<table><tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr>` +
    rowsData.map((cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") + "</table>";
}
