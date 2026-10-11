# P0017.1 订阅 / 频率 / 存储限制核实

**建立**：2026-10-11（P0017.1 §5 要求的"先核实再定参数"）
**核实状态口径**：
- `[代码]` 已在仓库连接器/解析器中存在并被既有真实 smoke 覆盖
- `[文档]` 来自 Binance USDⓈ-M Futures 公开文档的稳定规则；**当前出口对 Binance 返回 HTTP 451，本轮无法做实时复核**（标记待真实出口复验）
- `[保守默认]` 未取得权威直证，取保守值并暴露为配置

> 真实 E2E（提案 §7-B）在非受限出口执行后，须用实测值回填本表并复诉。

## 1. WebSocket 订阅

| 项 | 值 / 结论 | 依据 |
|---|---|---|
| 合流入口 | `<host>/stream?streams=a/b/c`，单连接可订阅多条 stream | `[代码]` `StreamTier.combined_stream_url`（endpoints.py:68），P0001.9.1 真实 smoke 用过 combined |
| 单连接 stream 上限（USDⓈ-M） | **200 条/连接**（合流 URL） | `[文档]` Binance Futures WS limits |
| 单 IP 并发连接上限 | 300（WS）；消息频率上限 1024 msg/s/IP | `[文档]`；超限返回 451/429 |
| depth 推送 | `@depth` 固定 100ms（旧 250/500ms 已下线）；`@depth50/100` 可选 | `[代码]` P0001.9.1.1 已核实必须用 `pu`；`[文档]` |
| markPrice | `@markPrice@1s`（或 3s） | `[代码]` 现有订阅 |

**保守默认（本阶段）**：
- 每条合流连接最多 **50 条 stream**（远低于 200 上限，给重连/突发留余量）`[保守默认]`
- 每个订阅 symbol 用 3 条 stream（depth/aggTrade/markPrice）⇒ **每连接最多 ~16 个 symbol**
- 默认订阅上限 `market.subscription.max_symbols = 16`（可配置上调，但不超过"连接预算"）`[保守默认]`
- 需要更多 symbol 时按 50 stream/连接**分批多连接**，连接数远低于 300 上限（编排器实现分批）

## 2. REST：发现与快照

| 项 | 值 / 结论 | 依据 |
|---|---|---|
| `GET /fapi/v1/exchangeInfo` | 返回全市场 `symbols[]`（数百个永续/季度合约），权重高，**不应频繁拉取** | `[代码]` 客户端存在；只用于单 symbol 规则 |
| 体量 | 全量响应较大（数十–数百 KB 级） | `[保守默认]`：发现缓存 TTL 默认 **6h**（交易对集合变化慢）；启动拉一次 |
| 筛选 | `status=TRADING` + `contractType` 永续（`PERPETUAL`）+ `quoteAsset` 允许集合 | 提案 §9 人类确认：默认 quote∈{USDT}、永续、TRADING、无流动性门槛 |
| depth 快照 REST | resync 时调用，受既有 `resync_cooldown_ms` 节流 | `[代码]` 现有逻辑复用 |

## 3. 存储

| 项 | 值 / 结论 |
|---|---|
| 布局 | `~/.probex/market-data/<venue>/<symbol>/<kind>/<yyyy-mm-dd>/*.jsonl`（提案 §2.2，人类确认） |
| 写入 | append-only，`O_APPEND` 单次写 + `fsync`；分片损坏 fail-closed（复用 JsonlEventWriter 纪律） |
| 分片 | 按 `(venue,symbol,kind,day)`；volume 主要来自 book 快照序列（depth 100ms ⇒ 仅订阅窗口内大量数据） |
| 保留 | 复用既有 RetentionPolicy 思路；**首期默认只保留显式订阅窗口的数据**，不做无限回填（避免磁盘无界） |
| 不回填历史 | Binance 公开 WS 只给实时；历史 K 线 REST 回填不在本阶段范围（避免引入权重成本与新契约） |

## 4. 待真实出口复验项（NOT RUN 清单）

- [ ] 实测 exchangeInfo 响应大小、TRADING 永续 USDT 对的实际数量
- [ ] 实测合流 16 symbol 的消息速率与 event lag（确认 50 stream/连接默认合理）
- [ ] 实测单 symbol 磁盘写入速率（book/trade）
- [ ] 451 解除后确认 `fstream.binance.com` 与文档入口一致
