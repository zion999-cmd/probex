# Probex 本地运行手册（Runbook）

本文件只讲**怎么在本机把 Probex 跑起来、怎么看、怎么停**。
治理规则（谁有权改什么、什么能做/不能做）见 `CLAUDE.md`；当前阶段与状态见 `context/status.json`、
`context/current_state.md`、`context/handoff.md`、`context/roadmap.md`。

---

## 0. 30 秒速查

| 我想…… | 命令 |
| --- | --- |
| **最快看到产品**（浏览器 + 图表 + 本地交易闭环） | `scripts/probex.sh demo` → 打开 `http://127.0.0.1:8897/` |
| **正式启动产品**（本地可写，PAPER） | `scripts/probex.sh up --mode paper` → 打开 `http://127.0.0.1:8899/` |
| **只回放、不下单**（REPLAY 观察模式） | `scripts/probex.sh up --mode replay` → 打开 `http://127.0.0.1:8898/` |
| 查看运行中的系统（只读） | `scripts/probex.sh status --port 8899` |
| 停止 | 在该终端按 `Ctrl-C`（优雅关闭） |
| 重新生成行情数据 | `python3 scripts/make_event_store.py ~/.probex/local-run/events.jsonl --hours 3 --force` |

前置：只需 **python3**（3.11+，仓库只用标准库）。仓库内**没有**任何第三方 Python 依赖；
`node` 仅在跑浏览器测试时需要。

---

## 1. 三个启动脚本

| 脚本 | 作用 | 默认端口 | 会写什么 |
| --- | --- | --- | --- |
| `scripts/probex.sh demo` | 一键：合成数据 → 本地 PAPER runtime → 等到出现一笔**模拟成交** → 打开浏览器 | 8897 | `~/.probex/local-run/demo.json` + `~/.probex/local-run/runs/` |
| `scripts/probex.sh up` | 产品入口（REPLAY / PAPER；默认 event-store；可 `--market-source binance-public` 吃真实持续公网行情，只读无需凭据） | paper 8899 / replay 8898 | `~/.probex/local-run/events.jsonl`（若缺）+ runs |
| `scripts/probex.sh status` | 只读查看运行中的 runtime（状态 / 事实 / blockers / runs） | 读 8899 | 不写 |
| `scripts/make_event_store.py` | 生成合成 event store（**试验数据**） | — | 你指定的路径 |

> **REPLAY vs PAPER**：REPLAY 是观察模式（`quoting=false`，零写）；PAPER 会真实走
> Market → Prediction → MakerPolicy → RiskGate → **本地**适配器 → 成交 → 账本 → PnL → UI/API。
> 两者都**不会**对真实交易所下单。

---

## 2. PAPER / REPLAY 详解

```bash
# 本地可写（默认端口 8899，3 小时合成数据）
scripts/probex.sh up --mode paper

# 自定义端口/时长/路径
scripts/probex.sh up --mode paper --port 9000 --hours 6 \
  --run-dir /tmp/probex-runs --event-store /tmp/probex-events.jsonl

# 用你自己的配置（默认 profiles/trial-local.json）
scripts/probex.sh up --mode paper --config-file my-profile.json

# 透传产品入口自己的参数（`--` 之后的都原样传给它）
scripts/probex.sh up --mode paper -- --config risk.max_position_qty=0.005
```

`probex.sh up` 支持的参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--mode` | `paper` | 只接受 `paper` / `replay` |
| `--port` | 8899 / 8898 | `0` = 临时端口（看启动日志里的 `url`） |
| `--hours` | `3` | event store 缺失时的合成时长 |
| `--symbol` | `BTCUSDT` | 交易标的 |
| `--local-dir` | `~/.probex/local-run`（可用 `PROBEX_LOCAL_DIR` 覆盖） | 本地工作目录 |
| `--event-store` | `<local-dir>/events.jsonl` | 行情事件存储（JSONL） |
| `--run-dir` | `<local-dir>/runs` | run registry（durable 记录） |
| `--config-file` | `profiles/trial-local.json` | 配置来源（FILE 层） |
| `-- ...` | — | 透传给 `python3 -m runtime.assembly` |

启动后终端会打印一行启动 JSON（含 `runtime_id` / `run_id` / `url`），并提示 `READY → 打开 http://…`。

### 2.0 真实持续公网行情（推荐有网络条件时使用）

```bash
# 真实 Binance 公开行情（无需凭据；持续更新 + 事实写入本地）
scripts/probex.sh up --mode paper --market-source binance-public
# 直接调用：python3 -m runtime.assembly --mode paper --symbol BTCUSDT --market-source binance-public
```
若当前出口被 Binance 限制（HTTP 451），产品不应退回伪造数据：connector health 会变为 FAILED/RECONNECTING；需更换出口（如不同地区隧道）。

### 2.1 直接调用产品入口（不使用脚本）

等价的最小命令：

```bash
python3 scripts/make_event_store.py ~/.probex/local-run/events.jsonl --hours 3   # 或自备 event store
python3 -m runtime.assembly --mode paper --symbol BTCUSDT \
  --config-file profiles/trial-local.json \
  --event-store ~/.probex/local-run/events.jsonl \
  --port 8899 --run-registry-dir ~/.probex/local-run/runs
```

---

## 3. 看什么

### 3.1 UI（浏览器，`http://127.0.0.1:<port>/`）

| Surface | Hash | 回答的问题 |
| --- | --- | --- |
| Monitor | `#/monitor` | 现在系统怎么样？（equity / position / 健康 / 四层状态） |
| Market | `#/market/live` | 市场现在发生了什么？（K 线 / 指标 / 语义标记 / 盘口 / 热图 / 预测） |
| Market（回放/审查） | `#/market/replay`、`#/market/run-review/<run_id>` | 重放控制；某个历史 run 的时间轴与图上定位 |
| Activity | `#/activity` | 系统为什么这样做？（decision 因果链 → risk → order → fill → accounting） |
| Performance | `#/performance` | 系统做得怎么样？（equity/回撤/暴露曲线、run 指标、对比） |
| System | `#/system/connections`、`#/system/health`、`#/system/risk`、`#/system/readiness`、`#/system/configuration`、`#/system/ops` | 为什么现在能/不能运行？ |

右上角 Assistant 抽屉提供确定性问答（不走 LLM）；`#/market/live` 上点 K 线的 semantic marker 可跳到
Activity / Evidence / Raw Facts。

### 3.2 REST API（只读，loopback 免认证）

| 端点 | 内容 |
| --- | --- |
| `GET /health/live` | 进程存活（免认证） |
| `GET /api/v1/status`、`/api/v1/snapshot` | 状态 / 全量读模型 |
| `GET /api/v1/market/candles?interval=1m&limit=240` | K 线（含服务端 `vwap` 与 `indicators.atr`） |
| `GET /api/v1/decisions/orders?decision_id=…`、`/api/v1/decisions/detail?decision_id=…` | 决策↔订单 / 因果链 |
| `GET /api/v1/facts/<kind>/<identity>` | 原始事实（order / fill / decision / prediction…） |
| `GET /api/v1/runs`、`/api/v1/runs/<id>/market` | run 列表 / 某 run 的行情事实 |
| `GET /api/v1/blockers`、`/api/v1/metrics`、`/api/v1/capabilities`、`/api/v1/reasons` | 为什么没动作 / 指标定义 / 能力清单 / 原因码解释 |

写语义：除本地 replay 控制与（未接线的）`/api/v1/runtime/stop` 外，非 GET 一律 `405`；
**没有任何下单/撤单端点**。

### 3.3 只读 CLI

```bash
python3 -m cli --api-url http://127.0.0.1:8899 status
python3 -m cli --api-url http://127.0.0.1:8899 snapshot --json
python3 -m cli --api-url http://127.0.0.1:8899 blockers        # 最有用：为什么现在什么都没做
python3 -m cli --api-url http://127.0.0.1:8899 runs --limit 5
python3 -m cli --api-url http://127.0.0.1:8899 run show <run_id>
python3 -m cli --api-url http://127.0.0.1:8899 actions         # 动作清单（CAPITAL 全部不可用）
```

子命令：`status market prediction strategy risk orders portfolio readiness evidence snapshot inspect
blockers metrics capabilities reasons runs execution ops actions action run explain report`。

---

## 4. 产物与目录

```text
~/.probex/local-run/               # 默认工作目录（仓库外；可用 PROBEX_LOCAL_DIR 覆盖）
├── events.jsonl                   # 合成 event store（REPLAY/PAPER 的输入）
├── demo.json                      # probex.sh demo 写下的事实摘要（mode/decisions/fills/equity/…）
└── runs/
    ├── index.jsonl                # run 索引（append-only）
    ├── active.json                # 运行中标记（含 PID 探活；无 TTL 真相）
    └── <run_id>.jsonl             # run 记录（COMPLETED / INCOMPLETE）
        <run_id>.market.jsonl      # run 级市值点（节流写入）
        <run_id>.facts.jsonl       # run 级 decision/order 事实
        <run_id>.trades.jsonl      # run 级真实逐笔（历史 run 的 K 线因此有成交量）
```

日志：单行 JSON（`ts/level/event/component/runtime_id/run_id/mode`），默认输出到终端；
`PROBEX_LOG_LEVEL` / `PROBEX_LOG_FILE` 可调，secret / signature / bearer 一律 redact。

---

## 5. 停止、崩溃与状态语义

| 情况 | 结果 |
| --- | --- |
| `Ctrl-C` / `SIGTERM` | **优雅关闭**：先 finalize run（`COMPLETED`）再退出 |
| `SIGKILL`（`kill -9`） | run 记为 `INCOMPLETE`（不伪造 `COMPLETED`）；`active.json` 保留，下次启动按 PID 探活判定 |
| 端口被占用 | 换 `--port`；或 `--port 0` 用临时端口（看启动日志 `url`） |
| 想看历史 run | `python3 -m cli --api-url … runs`，或 UI `#/performance`、`#/market/run-review/<run_id>` |

---

## 6. 配置、认证与覆盖优先级

优先级固定为 **CLI > ENV > FILE > CONSTRUCTOR**（`product/provenance.py`）：

```bash
--config key=value                       # CLI 层（可重复）
PROBEX_CONFIG_<key>=value                # ENV 层
--config-file profile.json               # FILE 层
```

- **默认只绑 loopback**（`127.0.0.1`），免 token。
- 非 loopback 必须**双重显式**：`--allow-non-loopback` + `--auth-token-ref env:PROBEX_API_TOKEN`，
  否则拒绝启动；此后所有 `/api/v1/*` 需 `Authorization: Bearer <token>`（`/health/live` 除外）。
- `profiles/trial-local.json` 自称 **LOCAL TRIAL ONLY**：其中的 `strategy.maker.*` / `risk.*` /
  `simulation.*` 都是**操作者提供的试验值**，不是生产策略或风险参数。

---

## 7. 常见错误

| 报错 | 原因 / 处理 |
| --- | --- |
| `--event-store is required for a real replay run` | REPLAY/PAPER 必须给 event store（用 `make_event_store.py` 或脚本自动生成） |
| `event-store runs require explicit display bounds …` | 配置缺 `projection.*`；`profiles/trial-local.json` 已包含，勿用空配置 |
| `prediction.provider=local_trial is … refused for TESTNET/LIVE` | LOCAL_TRIAL provider 只允许 REPLAY/PAPER（刻意） |
| `error: --mode must be paper or replay` | 脚本只支持本地模式；TESTNET/LIVE 见 §8 |
| `runtime 未响应：http://127.0.0.1:8899` | runtime 没起或端口不对；看启动日志 |
| `UNKNOWN` / `BLOCKING VENUE_FACTS_UNAVAILABLE` | 缺真实 venue 限额/用量证据 ⇒ 按设计 fail closed；本地 run 属正常 |

---

## 8. 边界：本手册**不**覆盖真实交易

- 本地脚本只启动 REPLAY/PAPER，写的是**本地**适配器；产品面**没有任何下单能力**
  （Action Gateway 中 CAPITAL 全部 `unavailable_by_design`）。
- **TESTNET 验收**是独立能力，需**双重显式**开关与仓库外凭据，且当前阶段
  （`proposals/P0001.16-venue-execution-productization.md`，状态 **实现中 / Deferred**）**未收口**，
  测试网还有真实残留仓位。**未经人类明确授权不要运行**：

  ```bash
  # ⚠️ 会向 Binance TESTNET 真实下单（BTCUSDT / ≤100 USDT / GTX 或 IOC）
  set -a; . ~/.probex/testnet.env; set +a
  export PROBEX_TESTNET_CONFIRM=1            # 缺它直接拒绝
  export PROBEX_TESTNET_ACCEPTANCE=1         # 只有需要真实成交(B)才需要
  python3 tests/acceptance/testnet_acceptance_run.py
  ```

- 真实市场数据：本手册的 event store 由 `tests/ui/market_fixture.py` **合成**，不是真实行情证据；
  真实数据需另行录制（`storage/events/writer.py::JsonlEventWriter`）。
- `LOCAL_TRIAL` prediction provider 是**确定性试验 provider**，不是已验证有效的预测模型；
  UI/API 会标注 `provider=LOCAL_TRIAL` / `model=local-trial-v1`。

---

## 9. 相关文件

| 文件 | 作用 |
| --- | --- |
| `CLAUDE.md` | 项目级执行规范（治理、Scope、Stop Condition） |
| `context/status.json` | `currentProposal`（当前唯一机器入口） |
| `context/current_state.md`、`context/handoff.md` | 已完成能力 / 最近实现会话 |
| `proposals/` | 各阶段 Proposal（实现契约） |
| `runtime/assembly.py` | 产品入口（composition root） |
| `scripts/` | 本手册的启动工具（非产品能力） |
