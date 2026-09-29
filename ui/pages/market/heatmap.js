/** L2 depth heatmap（Canvas，无第三方库；只在浏览器里"画"，不重算任何市场事实）。 */
export const HEATMAP_NOTE = "depth disappearance is NOT fill evidence (L2 change != fill evidence)";

export function drawHeatmap(canvas, depth) {
  const ctx = canvas.getContext("2d");
  const cells = (depth && depth.cells) || [];
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!cells.length) {
    ctx.fillStyle = "#d8a657";
    ctx.font = "12px ui-monospace, monospace";
    ctx.fillText("UNKNOWN (no depth projection)", 8, 20);
    return;
  }
  const times = [...new Set(cells.map((c) => c.bucket_ts))].sort((a, b) => a - b);
  const prices = cells.map((c) => c.price);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const maxQty = Math.max(...cells.map((c) => c.quantity), 1e-9);
  const padLeft = 60;
  const padTop = 8;
  const width = canvas.width - padLeft - 8;
  const height = canvas.height - padTop - 18;
  const x = (ts) => padLeft + (times.indexOf(ts) / Math.max(1, times.length - 1)) * width;
  const y = (price) => padTop + (1 - (price - minPrice) / Math.max(1e-9, maxPrice - minPrice)) * height;
  const cellW = Math.max(2, width / Math.max(1, times.length));
  const cellH = Math.max(2, height / 40);
  for (const cell of cells) {
    const intensity = Math.min(1, cell.quantity / maxQty);
    ctx.fillStyle = cell.side === "bid"
      ? `rgba(126, 198, 153, ${0.15 + 0.85 * intensity})`
      : `rgba(224, 108, 117, ${0.15 + 0.85 * intensity})`;
    ctx.fillRect(x(cell.bucket_ts), y(cell.price) - cellH / 2, cellW, cellH);
  }
  ctx.strokeStyle = "#7ec699";
  ctx.beginPath();
  (depth.bid_series || []).forEach(([ts, price], index) => {
    const px = x(ts); const py = y(price);
    if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  });
  ctx.stroke();
  ctx.strokeStyle = "#e06c75";
  ctx.beginPath();
  (depth.ask_series || []).forEach(([ts, price], index) => {
    const px = x(ts); const py = y(price);
    if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  });
  ctx.stroke();
  ctx.fillStyle = "#8b93a1";
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillText(maxPrice.toFixed(1), 4, padTop + 8);
  ctx.fillText(minPrice.toFixed(1), 4, padTop + height);
}
