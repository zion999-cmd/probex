# Decisions

## D-001 领域优先的模块布局，不新建 `core/` 等通用桶目录

**日期**：2026-09-28
**状态**：生效
**约束来源**：CLAUDE.md §15（不得新建无明确领域归属的通用垃圾桶目录，且点名 `core/`）
**背景**：P0001 总架构文本给出的目录结构建议以 `core/market/...` 为顶层。该文本使用「建议」措辞，并非强制要求；CLAUDE.md §15 明确点名排除 `core/`。
**决策**：市场数据核心放在顶层领域包 `market/`（`market/events/`、`market/book/`、`market/health/`）；交易所接入放在 `connectors/binance/market_data/`。不创建 `core/`、`services/`、`managers/`、`utils/`。
**影响**：后续阶段的 `prediction/`、`strategy/`、`policy/`、`execution/`、`portfolio/`、`risk/`、`storage/` 等领域包同样置于顶层，不再包一层通用父目录。

## D-002 测试使用标准库 unittest，不引入 pytest

**日期**：2026-09-28
**状态**：生效（待人类许可后可变更）
**约束来源**：CLAUDE.md §16（新增第三方依赖需当前 Proposal 明确授权）、§29（最少新依赖）
**背景**：P0001 总架构授权了 `tests/` 目录结构，但未授权任何第三方依赖。P0001.1 未授权新增依赖，`unittest` 足以完成全部验收。
**决策**：P0001.1 的实现与测试零第三方依赖，使用标准库 `unittest`；测试从仓库根目录以 `python3 -m unittest discover -s tests -t .` 运行，因此暂不创建 `pyproject.toml`。
**影响**：引入 pytest 或打包配置需先获得人类授权。若后续阶段授权，测试用例的 `unittest.TestCase` 形式可被 pytest 直接收集，迁移成本低。

## D-003 盘口序号连续性以交易所 `U`/`u` 区间判定

**日期**：2026-09-28
**状态**：生效
**背景**：Binance USDⓈ-M `depthUpdate` 同时提供 `U`（首个更新 id）、`u`（末个更新 id）、`pu`（上一事件的 `u`）。
**决策**：连续性判定规则为 `first_update_id <= last_applied_update_id + 1 <= last_update_id`，不引入 `pu`；`pu` 不进入字段集。
**理由**：`U`/`u` 区间规则对现货与合约增量都成立，避免把交易所可选字段变成核心契约；同一规则同时覆盖「重复 / 过期」与「gap」两种判定。
**影响**：若将来需要更严格的连续性校验，可在归一化层增加字段，而不改动 `OrderBook` 的判定规则。

## D-004 Event Store 的排序契约落地为 recorded ordinal

**日期**：2026-09-28
**状态**：生效
**背景**：P0001.2 原文的排序契约为「1. logical stream order、2. sequence、3. recorded ordinal（最终 tie-breaker）」，但未定义三者在一个单文件 store 中的具体关系。
**决策**：单文件 store 内 ordinal 严格递增且唯一，它就是 logical stream order；Replay 顺序 = ordinal 顺序（即文件 append 顺序）。时间戳不参与排序（有专项测试）。`sequence` 不参与排序，仅由下游 `MarketBook` 做连续性判定。
**理由**：该落地方式无需引入提案未要求的额外排序字段，且完全满足「时间戳相同也能唯一确定顺序」的要求。
**影响**：跨文件合并、或需要按 `sequence` 重排的场景需新提案；`EventReader` 的严格递增校验是该契约的执行点。

## D-005 Event Store 记录完整性使用内容寻址 event_id

**日期**：2026-09-28
**状态**：生效
**背景**：提案要求记录必须含 `event_id`，且 store 必须 immutable、禁止原地修改历史。
**决策**：`event_id` = `sha256:` + canonical JSON（不含 ordinal）摘要；读取端重算并比对，不一致抛 `EventIntegrityError`。
**影响**：历史被手改可被检测；`event_id` 相同不代表同一记录（同一事件重复写入时以 ordinal 区分）。
