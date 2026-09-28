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
P0001.5   Accounting + Risk
P0001.6   Paper Execution + Order Lifecycle
P0001.7   Market Making
P0001.8   Event-level Fill Simulation
P0001.9   Binance Live
P0001.10  Product API / UI / Reports
```

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
| P0001.5 – P0001.10 | 未开始（待各自独立子提案落盘） |
