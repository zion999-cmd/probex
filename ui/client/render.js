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
  // F3：factRows 也必须能处理嵌套对象/数组（不再出现 [object Object]）
  return rows(Object.entries(object || {}).map(([k, v]) => [k, valueView(v)]));
}

/** F3：任意对象 → 可读 key/value 区块（同 valueView，便于页面直接用）。 */
export function valueRows(object) {
  return valueView(object || {});
}

/** F-09：reason code 单元格——原始 code 永远保留，另附人类解释（未知 ⇒ 暂无解释）。
 *
 * F7：`null`/`""` 表示"本阶段没有 reason code"，不得伪装成 UNKNOWN code。
 */
export function reasonCell(code, catalog = []) {
  if (code === null || code === undefined || code === "") {
    return '<span class="muted">— no reason code</span>';
  }
  const entry = (catalog || []).find((item) => item.reason_code === code);
  const raw = `<span class="bad">${escapeHtml(String(code))}</span>`;
  if (!entry) {
    return `${raw} <span class="unknown">暂无解释 / not catalogued</span>`;
  }
  return `${raw} <span class="known">${escapeHtml(entry.title)}</span>` +
    `<div class="muted">${escapeHtml(entry.explanation)} — ${escapeHtml(entry.suggested_next_step)}</div>`;
}

/** F1：position 三态（known qty / flat / unknown），不再出现布尔 false。 */
export function positionFact(value) {
  if (value && value.known && Number(value.value) === 0) {
    return '<span class="known">flat (0)</span>';
  }
  return fact(value);
}

/** F3：通用 typed value renderer —— 任何 JSON 值都渲染成可读内容，绝不出现 `[object Object]`。
 *
 * - Fact 解包（UNKNOWN 带 reason）；
 * - 对象：标量先出表格（人要看的摘要），嵌套对象用 `<details>` 折叠；
 * - 数组：标量数组同行；对象数组表格化。
 */
export function valueView(value) {
  if (value === null || value === undefined) return '<span class="unknown">UNKNOWN</span>';
  if (typeof value === "object" && "known" in value && "value" in value) {
    if (!value.known) return `<span class="unknown">UNKNOWN (${escapeHtml(value.reason || "unknown")})</span>`;
    return valueView(value.value);
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return '<span class="known">none</span>';
    if (value.every((item) => item === null || typeof item !== "object")) {
      return `<span class="known">${escapeHtml(value.map((item) => String(item)).join(", "))}</span>`;
    }
    return table(["value"], value.map((item) => [valueView(item)]));
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (!entries.length) return '<span class="known">none</span>';
    const scalars = entries.filter(([, item]) => item === null || typeof item !== "object");
    const nested = entries.filter(([, item]) => item !== null && typeof item === "object");
    let html = scalars.length
      ? table(["key", "value"], scalars.map(([key, item]) => [escapeHtml(key), valueView(item)])) : "";
    for (const [key, item] of nested) {
      html += `<details class="nested"><summary>${escapeHtml(key)}</summary>${valueView(item)}</details>`;
    }
    return html || '<span class="known">none</span>';
  }
  return `<span class="known">${escapeHtml(String(value))}</span>`;
}

/** F3：原始 JSON 只放在 detail 里，不充当默认产品界面。 */
export function jsonDetail(value, label = "raw JSON") {
  return `<details class="raw"><summary>${escapeHtml(label)}</summary>` +
    `<pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></details>`;
}

export function table(headers, rowsData) {
  if (!rowsData.length) return '<div class="row"><span class="k">rows</span><span class="v known">0</span></div>';
  return `<table><tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr>` +
    rowsData.map((cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") + "</table>";
}
