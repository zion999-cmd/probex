# Probex

量化交易系统（当前范围：`BTCUSDT` 永续，REPLAY / PAPER 本地闭环 + 只读产品面）。

## 快速开始

```bash
scripts/probex.sh demo       # 合成数据 + 本地 PAPER 闭环，然后打开 http://127.0.0.1:8897/
```

正式启动产品（本地可写 / 只回放）：

```bash
scripts/probex.sh up --mode paper   # → http://127.0.0.1:8899/
scripts/probex.sh up --mode replay  # → http://127.0.0.1:8898/（观察模式，零写）
```

只读查看运行中的系统：

```bash
scripts/probex.sh status --port 8899
```

**运行手册（参数、UI 路由、API、产物目录、停止语义、常见错误）：[`docs/RUNBOOK.md`](docs/RUNBOOK.md)**

## 边界（先读这一节）

- 本地脚本只启动 **REPLAY / PAPER**，写的是**本地**适配器；产品面**没有任何下单能力**
  （Action Gateway 中 CAPITAL 全部 `unavailable_by_design`）。
- **TESTNET 真实写**是独立能力（`proposals/P0001.16-*.md`，状态 **实现中 / Deferred**），
  需双重显式开关 + 仓库外凭据，**未收口**；未获授权不要运行（见 RUNBOOK §8）。
- `profiles/trial-local.json` 是 **LOCAL TRIAL** 试验配置，不是生产策略/风险参数。
- `LOCAL_TRIAL` prediction provider 是确定性试验 provider，**不是**已验证有效的预测模型。
- 仓库只用 Python 标准库（`node` 仅浏览器测试需要）。

## 目录导航

| 路径 | 内容 |
| --- | --- |
| `market/` `portfolio/` `risk/` `execution/` `strategy/` `prediction/` | 领域代码（各自由唯一 Owner 持有状态） |
| `connectors/` `venue/` `domain/` | 交易所连接、venue 契约、instrument 域 |
| `runtime/` `readiness/` `live/` `execution_safety/` | 组装、运行闭环、就绪门、执行安全 |
| `product/` `api/` `ui/` `assistant/` `actions/` `reports/` `cli/` | 只读产品面 |
| `storage/` `context/` `proposals/` `artifacts/` `scripts/`（唯一启动工具 `probex.sh`） `tests/` | 持久化、治理上下文、提案、验收证据、启动工具、测试 |

## 治理与当前状态

- 执行规范：`CLAUDE.md`
- 当前 Proposal（唯一机器入口）：`context/status.json` → `currentProposal`
- 已完成能力 / 最近会话：`context/current_state.md`、`context/handoff.md`
- 阶段路线：`context/roadmap.md`
