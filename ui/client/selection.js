/** 图表/页面选中上下文（供 Assistant drawer 读取；不拥有任何业务事实）。 */
const selection = Object.freeze({ surface: "monitor" });

let current = { surface: "monitor" };

export function updateSelection(patch) {
  current = Object.assign({}, current, patch || {});
  if (typeof window !== "undefined" && window.dispatchEvent) {
    window.dispatchEvent(new CustomEvent("probex:selection", { detail: { ...current } }));
  }
  return current;
}

export function getSelection() {
  return { ...current };
}

export function selectionQuery() {
  const entries = [];
  for (const [key, value] of Object.entries(current)) {
    if (value === null || value === undefined || value === "") continue;
    entries.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return entries.join("&");
}

export { selection };
