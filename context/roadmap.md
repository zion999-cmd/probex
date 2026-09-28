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
P0001.9.2 Private Account + User Stream Validation ← 已完成（TESTNET_PRIVATE_VALIDATED / MAINNET_PRIVATE_NOT_YET_VALIDATED）
P0001.9.2.1 Private Connectivity Contract Audit & CCXT Fit ← 已完成（裁决 KEEP_NATIVE_PRIVATE）
P0001.9.3 Startup Recovery + Account Reconciliation ← 已完成（测试网真实只读验收 PASS）
P0001.9.3.1 Recovery Contract Closure             ← 已完成（ownership / unresolved fills / 断线接线收口）
P0001.9.3.2 Discontinuity Notification Reliability ← 已完成（observer 故障隔离 + 审计 + 文档清理）
P0001.9.4 Live Readiness Gate                     ← 已完成（RECOVERED ≠ LIVE_READY；测试网 readonly 验收 PASS）
P0001.9.4.1 Historical Risk Bootstrap              ← 已完成（income 恢复 daily PnL；drawdown 仍 UNKNOWN）
P0001.9.4.1.1 Full Testnet Readiness Integration Validation ← 已完成（唯一 blocker = HISTORICAL_DRAWDOWN_UNKNOWN）
P0001.9.4.2 Persistent Equity High-Watermark      ← 已完成（Testnet 跨进程验收：readiness reasons = []）
P0001.10  Product API / UI / Reports       ← 未启动（待正式 Proposal）
```

> **人类裁决（2026-09-28）**：原 P0001.9「Binance Live」拆为三个子阶段并依次串行执行 ——
> 该层第一次接触真实外部状态，先把公网行情链单独验证，不与真实下单混在一起；
> 且 Binance 2026 年调整了 USDⓈ-M WebSocket 入口（见 handoff「待核实的外部事实」）。
> 提案落盘情况：**P0001.9.1 + P0001.9.1.1 均已完成**（真实公网验收通过）。
> P0001.9.2 已完成（测试网已验证、主网未验证）；P0001.9.3 已完成（测试网真实只读读取 + 恢复门验收通过）。
> 下一阶段（P0001.10）尚未落盘提案；实现 Agent 不得从本行推断任务（`status.json.currentProposal` 为 `null`）。

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
| P0001.9.2 | 已完成（测试网真实事件链 + 状态串 `TESTNET_PRIVATE_VALIDATED / MAINNET_PRIVATE_NOT_YET_VALIDATED`） |
| P0001.9.2.1 | 已完成（裁决 `KEEP_NATIVE_PRIVATE`） |
| P0001.9.3 | 已完成（测试网真实验收 PASS：RECOVERED / 无 synthetic Fill / foreign open 0） |
| P0001.9.3.1 | 已完成（严格 ownership + `BLOCKED: UNRESOLVED_FILLS` + runtime→recovery 自动失效接线） |
| P0001.9.3.2 | 已完成（listener 异常隔离 + 失败计数审计 + current_state/handoff 失效表述清理） |
| P0001.9.4 | 已完成（readiness gate + 交易所可用余额 + drawdown 事实链修正 + 时钟校正；测试网验收 BLOCKED 且可解释） |
| P0001.9.4.1 | 已完成（真实 income 历史恢复 daily PnL：−1.27395553 USDT；drawdown/peak 仍 UNKNOWN ⇒ 仍 BLOCKED） |
| P0001.9.4.1.1 | 已完成（真实集成验证：market_ready 由事实判定、corrected=raw+offset 成立、唯一 blocker = drawdown） |
| P0001.9.4.2 | 已完成（durable HWM：activate→persist→restart→recovery→restore→readiness，Testnet `live_ready` / reasons `[]`） |
| P0001.10 | 未开始（待正式提案落盘） |
