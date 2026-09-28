# Roadmap

## 阶段路线

来源：`proposals/P0001.1-market-event-l2-book-bookhealth.md` 第 2 节（原 P0001 总架构提案，人类已批准）。

```text
P0001.1   Market Event + L2 Book + BookHealth   ← 已完成
P0001.2   Event Store + Deterministic Replay    ← 已完成
P0001.3   Feature / MarketState                 ← 已完成
P0001.4   Jev Prediction Runtime                ← 已完成
P0001.4.1 Real Jev Transport Validation         ← 已完成（CONTRACT_MISMATCH / PROVIDER_UNSUITABLE）
P0001.4.2 Native Typed Jev Provider             ← 已完成（热路径 = /v1/systemone）
P0001.5   Accounting + Risk Core                 ← 已完成
P0001.6   Order Lifecycle + Paper Execution      ← 已完成
P0001.6.1 Uncertain Order Exposure               ← 已完成
P0001.7   Market Making Policy                   ← 已完成
P0001.7.1 Prediction Outage Reduce-only Continuity ← 已完成
P0001.8   Event-level Fill Simulation            ← 已完成
P0001.9.1 Public Market Data Live                ← 已完成（真实公网验收通过）
P0001.9.1.1 Futures Depth Continuity Verification ← 已完成（pu 判据修正）
P0001.9.2 Private Execution + User Stream        ← 未开始
P0001.9.3 Startup Recovery + Account Reconciliation ← 未开始
P0001.5   Accounting + Risk
P0001.6   Paper Execution + Order Lifecycle
P0001.7   Market Making
P0001.8   Event-level Fill Simulation
P0001.9.1 Public Market Data Live
P0001.9.2 Private Execution + User Stream
P0001.9.3 Startup Recovery + Account Reconciliation
P0001.10  Product API / UI / Reports
```

> **人类裁决（2026-09-28）**：原 P0001.9「Binance Live」拆为三个子阶段并依次串行执行 ——
> 该层第一次接触真实外部状态，先把公网行情链单独验证，不与真实下单混在一起；
> 且 Binance 2026 年调整了 USDⓈ-M WebSocket 入口（见 handoff「待核实的外部事实」）。
> 提案落盘情况：**P0001.9.1 + P0001.9.1.1 均已完成**（真实公网验收通过）。
> P0001.9.2 / P0001.9.3 **未落盘**。实现 Agent 不得从本行推断任务（`status.json.currentProposal` 为 `null`）。

依赖：阶段顺序为串行依赖，前一阶段验收完成后才进入下一阶段。P0001.1 – P0001.4 完全不碰交易。

## 阶段状态

| 阶段 | 状态 |
| --- | --- |
| P0001.1 | 已完成（2026-09-28，全部 Success Criteria PASS） |
| P0001.2 | 已完成（2026-09-28，全部 Success Criteria PASS） |
| P0001.3 | 已完成（2026-09-28，全部 Success Criteria PASS） |
| P0001.4 | 已完成（2026-09-28，全部 Success Criteria PASS） |
| P0001.4.1 | 已完成（结论：`OpenRouterTransport` VALIDATED；`typesafe/jev-router` REJECTED_FOR_NOW） |
| P0001.4.2 | 已完成（typed System One 热路径；真实 latency 409–462 ms） |
| P0001.5 | 已完成（Fill 事实源 + 账本 + RiskGate） |
| P0001.6 | 已完成（订单生命周期 + PaperBroker + reconciliation） |
| P0001.6.1 | 已完成（LOST/资料不足暴露 fail-closed） |
| P0001.7 | 已完成（Maker 策略层：报价/规模/库存/生命周期） |
| P0001.7.1 | 已完成（prediction 中断时禁止增加暴露、允许新增 reduce-only） |
| P0001.8 | 已完成（事件级成交模拟：aggressor trade 证据 + 队列近似 + 延迟 + 挂起；1104 条测试通过） |
| P0001.9.1 | 已完成（真实公网验收 PASS：无凭据完成 6 个表面；修复后 gap 0 / resync 1 / HEALTHY 100%） |
| P0001.9.1.1 | 已完成（Futures 连续性判据 = `pu == prev.u`；锚点用跨锚点条件） |
| P0001.9.2 | 未开始（提案未落盘） |
| P0001.9.3 | 未开始（提案未落盘） |
| P0001.5 – P0001.10 | 未开始（待各自独立子提案落盘） |
