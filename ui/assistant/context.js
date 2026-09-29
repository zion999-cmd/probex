/** Assistant context：从 URL hash 派生当前上下文（Surface + 选中对象），不复制领域状态。 */
export function currentSelection(hash = window.location.hash) {
  const parts = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean);
  const surface = parts[0] || "monitor";
  const selection = {};
  for (let index = 1; index < parts.length - 1; index += 2) {
    const key = parts[index];
    if (["run", "decision", "order", "fill"].includes(key)) selection[key] = parts[index + 1];
  }
  return { surface, selection };
}
