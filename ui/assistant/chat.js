/**
 * Assistant 自由文本问答（P0001.12.3 §7/§8/§9）——**确定性路由，不接 LLM、不造事实**。
 *
 * 设计约束：
 * - 只使用已存在的 Product API（snapshot / blockers / orders / runs / execution.* / explain / actions）；
 * - 每个答案都携带**数据来源**（endpoint），并保留 UNKNOWN / UNAVAILABLE 原样；
 * - 涉及资本动作（下单/撤单/改杠杆/改风控）**不做**，明确回答 unavailable_by_design 并给出 manifest 依据；
 * - 绝不声称执行了任何动作：动作只能通过 Action Gateway 按钮单独执行并回显真实结果。
 */
import { ENDPOINTS, explain, fetchJson, fetchSnapshot } from "/ui/client/api.js";

const t = (value) => (value === null || value === undefined ? "" : String(value));
const factText = (value, label = "UNKNOWN") => {
  if (!value || typeof value !== "object" || !("known" in value)) return factText({ known: true, value }, label);
  if (!value.known) return `${label}${value.reason ? `（${value.reason}）` : ""}`;
  return String(value.value);
};

function has(question, ...words) {
  const q = question.toLowerCase();
  return words.some((w) => q.includes(w.toLowerCase()));
}

/** 阻塞项：原因 + 影响范围 + 下一步（catalog），全部来自真实 blockers 投影。 */
async function answerBlockers(question) {
  const payload = await fetchJson(ENDPOINTS.blockers);
  const blockers = payload.blockers || [];
  if (!blockers.length) {
    return { text: "当前没有 blocker：没有阻塞运行或操作的问题。", sources: [ENDPOINTS.blockers] };
  }
  const lines = blockers.map((b) => {
    const explanation = b.explanation || {};
    return `[${b.severity}] ${b.owner}:${b.reason_code} — ${b.message}` +
      (explanation.suggested_next_step ? `；下一步：${explanation.suggested_next_step}` : "");
  });
  return {
    text: `当前有 ${blockers.length} 个 blocker：\n${lines.join("\n")}`,
    sources: [ENDPOINTS.blockers],
    surfaceHint: "system/risk",
  };
}

/** 系统当前状态（Monitor 的 30 秒摘要）。 */
async function answerStatus() {
  const s = await fetchSnapshot();
  const id = s.runtime || {};
  const market = s.market || {};
  const blockers = (s.blockers || []).filter((b) => b.severity === "BLOCKING");
  return {
    text: `mode=${t(id.mode)} · environment=${t(id.environment)} · venue=${t(id.venue)} · symbol=${t(id.symbol)} · ` +
      `runtime=${t(id.runtime_id)}；market=${market.healthy && market.healthy.known ? (market.healthy.value ? "HEALTHY" : "UNHEALTHY") : "UNKNOWN"}，` +
      `data_ts=${factText(id.data_timestamp)}；BLOCKING=${blockers.length}。`,
    sources: [ENDPOINTS.snapshot],
    surfaceHint: "monitor",
  };
}

async function answerMarket() {
  const [snapshot, health] = await Promise.all([fetchSnapshot(), fetchJson(ENDPOINTS.marketHealth)]);
  const market = snapshot.market || {};
  return {
    text: `market.healthy=${market.healthy && market.healthy.known ? market.healthy.value : "UNKNOWN"}；` +
      `book health=${factText((health.book_health || {}).state, "UNKNOWN")}；` +
      `spread=${factText(health.spread, "UNKNOWN")}；mid=${factText(health.mid, "UNKNOWN")}；` +
      `data_ts=${factText((snapshot.runtime || {}).data_timestamp)}`,
    sources: [ENDPOINTS.snapshot, ENDPOINTS.marketHealth],
    surfaceHint: "market/live",
  };
}

/** 执行安全（P0001.13）：health / limits / rate-limit / latency / anomalies / reconciliation。 */
async function answerExecution(question) {
  const wanted = [
    ["health", ENDPOINTS.executionHealth, "execution health"],
    ["limits", ENDPOINTS.executionLimits, "venue limits"],
    ["rate", ENDPOINTS.executionRateLimits, "rate limits"],
    ["latency", ENDPOINTS.executionLatency, "latency"],
    ["anomal", ENDPOINTS.executionAnomalies, "anomalies"],
    ["reconcil", ENDPOINTS.executionReconciliation, "reconciliation"],
  ];
  const picked = wanted.filter(([key]) => has(question, key)) ;
  const targets = picked.length ? picked : [wanted[0]];
  const sources = [];
  const lines = [];
  for (const [key, path] of targets) {
    try {
      const payload = await fetchJson(path);
      sources.push(path);
      const value = payload[key] || payload.health || payload.limits || payload.latency ||
        payload.rate_limits || payload.anomalies || payload.reconciliation || payload;
      lines.push(`${key}: ${JSON.stringify(value).slice(0, 400)}`);
    } catch (error) {
      sources.push(path);
      lines.push(`${key}: UNAVAILABLE (${String(error)})`);
    }
  }
  return { text: lines.join("\n"), sources, surfaceHint: "system/execution" };
}

/** 订单：活跃订单 + （可定位时）单个订单的真实解释。 */
async function answerOrders(question, ctx) {
  const { execution } = await fetchJson(ENDPOINTS.orders);
  const orders = execution.active_orders || [];
  const sources = [ENDPOINTS.orders];
  if (!orders.length) {
    return { text: "当前没有挂单（active orders = 0）。", sources, surfaceHint: "activity/orders" };
  }
  const lines = orders.slice(0, 5).map((o) => `${o.client_order_id} ${o.side} ${o.status} ` +
    `px=${factText(o.price)} qty=${factText(o.quantity)} filled=${factText(o.filled_quantity)} ` +
    `decision=${factText(o.decision_id)}`);
  let extra = "";
  const selection = ctx && ctx.selection ? ctx.selection : {};
  const target = selection.order || (orders[0] && orders[0].client_order_id);
  if (target) {
    try {
      const result = await explain("order", target, { surface: "activity" });
      const answers = result.answers || {};
      extra = answers.selected_order_state ? `\n${target}: ${answers.selected_order_state}` : "";
      sources.push(ENDPOINTS.assistantExplain);
    } catch (error) {
      extra = `\n${target}: UNAVAILABLE (${String(error)})`;
    }
  }
  return { text: `${orders.length} 个挂单：\n${lines.join("\n")}${extra}`, sources, surfaceHint: "activity/orders" };
}

/** 决策：解释当前/已选决策（真实 correlation，不做字符串猜测）。 */
async function answerDecision(question, ctx) {
  const selection = (ctx && ctx.selection) || {};
  const identity = selection.decision || selection.order || selection.fill || "";
  if (!identity) {
    return { text: "没有选中的决策/订单：请先在 Activity 选中一条身份（或打开某个 decision 的 drill-down），" +
      "再问“为什么这样决策”。", sources: [], surfaceHint: "activity" };
  }
  const kind = selection.decision ? "decision" : (selection.fill ? "fill" : "order");
  try {
    const result = await explain(kind, identity, { ...selection, surface: (ctx && ctx.surface) || "activity" });
    const answers = result.answers || {};
    const text = Object.entries(answers).map(([k, v]) => `${k}: ${v}`).join("\n");
    return { text: text || `UNKNOWN（记录的 facts 中没有可解释内容）· ${kind}:${identity}`,
      sources: [ENDPOINTS.assistantExplain], surfaceHint: "activity" };
  } catch (error) {
    return { text: `UNAVAILABLE (${String(error)})`, sources: [ENDPOINTS.assistantExplain] };
  }
}

/** 表现：run 摘要（无事实时显式 UNKNOWN，不给演示数字）。 */
async function answerPerformance() {
  const sources = [];
  let summary = "";
  try {
    const payload = await fetchJson(ENDPOINTS.runSummary);
    sources.push(ENDPOINTS.runSummary);
    const metrics = payload.summary || payload;
    summary = JSON.stringify(metrics).slice(0, 500);
  } catch (error) {
    sources.push(ENDPOINTS.runSummary);
    summary = `UNAVAILABLE (${String(error)})`;
  }
  let runs = "";
  try {
    const payload = await fetchJson(ENDPOINTS.runs);
    sources.push(ENDPOINTS.runs);
    const list = payload.runs || [];
    runs = list.slice(0, 5).map((r) => `${r.run_id} ${r.mode || ""} ${r.status || ""}`).join("; ");
  } catch (error) {
    runs = `UNAVAILABLE (${String(error)})`;
  }
  return { text: `run summary: ${summary}\nruns: ${runs}`, sources, surfaceHint: "performance" };
}

/** 标的/venue：引用价与身份来源（P0001.15 §27）。 */
async function answerInstrument() {
  const s = await fetchSnapshot();
  const instrument = s.instrument || {};
  return {
    text: `instrument=${factText(instrument.instrument_id)} · asset_class=${factText(instrument.asset_class)} · ` +
      `product_type=${factText(instrument.product_type)} · venue=${factText(instrument.venue_id)}` +
      (s.reference_price ? `；MARK=${factText(s.reference_price)}（source=${factText(s.reference_price_source)}）` : ""),
    sources: [ENDPOINTS.snapshot],
    surfaceHint: "market/live",
  };
}

/** 资本动作：明确拒绝并给出 manifest 依据（不解释成"稍后可用"）。 */
function answerCapital() {
  return {
    text: "资本类动作（下单 / 撤单 / 买卖 / 杠杆 / 仓位 / 风控参数 / 强制进出场）在设计中全部 " +
      "unavailable_by_design：助手不会、也无法执行它们。可执行动作以 Action Manifest " +
      "（capabilities 页面可见）的 availability 为准；只有其中标记 AVAILABLE 的动作才会出现在动作按钮里。",
    sources: [ENDPOINTS.actions],
    surfaceHint: "system/capabilities",
  };
}

/** 未识别问题的诚实兜底：给出 snapshot 摘要 + 可问的问题，不编造。 */
async function answerFallback(question, ctx) {
  const status = await answerStatus();
  return {
    text: `我没有匹配到更具体的意图，先给当前事实：\n${status.text}\n` +
      `可以问：blockers / health / market / 订单 / 决策解释 / 执行延迟 / 对账 / run 表现 / 标的与 MARK。`,
    sources: status.sources,
  };
}

/**
 * 主入口：把一条自由文本问题路由到真实只读能力。
 * 返回 { text, sources, actions, surfaceHint }；`actions` 只给动作 id，由 Manifest 校验后才可点。
 */
export async function answerQuestion(question, ctx = {}) {
  const q = String(question || "").trim();
  if (!q) return { text: "请输入问题。", sources: [] };
  // 路由纪律：“为什么没有下单”是**诊断**问题，不得因包含“下单”二字被当作资本动作请求。
  if (isCapitalRequest(q)) return { ...answerCapital(), actions: [] };
  if (has(q, "blocker", "阻塞", "为什么不能", "为什么没", "为什么没有", "没下单", "没有下单",
      "卡的", "卡住")) {
    const blockers = await answerBlockers(q);
    if (has(q, "下单", "order")) {
      const orders = await answerOrders(q, ctx);
      return { ...blockers, text: `${blockers.text}\n\n订单事实：${orders.text}`,
        sources: [...blockers.sources, ...orders.sources] };
    }
    return blockers;
  }
  if (has(q, "execution", "执行", "延迟", "latency", "限流", "rate", "venue limit", "对账", "reconcil",
      "anomal")) return answerExecution(q);
  if (has(q, "market", "行情", "盘口", "book", "spread", "mark", "数据新鲜", "fresh")) return answerMarket();
  if (has(q, "订单", "order")) return answerOrders(q, ctx);
  if (has(q, "决策", "decision", "为什么这样", "为什么交易", "为什么取消", "why")) return answerDecision(q, ctx);
  if (has(q, "run", "表现", "收益", "pnl", "performance", "绩效")) return answerPerformance();
  if (has(q, "标的", "instrument", "perp", "spot", "合约", "交易对", "symbol")) return answerInstrument();
  if (has(q, "状态", "status", "系统", "system", "health", "健康", "怎么样", "情况")) return answerStatus();
  return answerFallback(q, ctx);
}

/**
 * 资本意图判定：必须**带请求语气**才成立。
 * “为什么现在没有下单？”→ 诊断（false）；“帮我下单 0.01 BTC” → 资本请求（true）。
 */
function isCapitalRequest(question) {
  if (/为什么|为何|怎么没|怎么会|why/i.test(question)) return false;
  const request = /(帮我|请|给我|我要|我想|麻烦|替我|马上|立即|place|submit|execute)/i.test(question);
  const capital = /(下单|挂单|买入|卖出|买|卖|平仓|撤单|撤掉|取消订单|杠杆|加仓|减仓|position size|risk limit)/i.test(question);
  return request && capital;
}

/**
 * 动作候选：只保留 Manifest 中真实存在且 AVAILABLE 的动作（按当前 surface 过滤）。
 * UI 不得自创动作；不可用动作只作为"为什么不可用"的说明出现。
 */
export async function manifestActions(surface) {
  const payload = await fetchJson(ENDPOINTS.assistantContext + `?surface=${encodeURIComponent(surface || "monitor")}`);
  return payload.suggested_actions || [];
}

export { factText, has };
