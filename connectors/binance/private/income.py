"""Binance `/fapi/v1/income` 只读事实归一化与分类（P0001.9.4.1）。

纪律（提案 §1 – §7）：

- **账户级**查询（不传 `symbol`）：daily loss 是账户级风险，不是单一交易对风险；
- **完整分页**：`rows < limit` 才算读完；页数超过显式上限、读取失败、窗口越界、字段未知 ⇒ **coverage 不完整**
  ⇒ 调用方必须让 `daily_pnl_known = false`（**绝不**用部分数据算一个"看起来对"的值）；
- **分类必须 fail closed**：交易类只有提案明确列出的 4 种；未知 `incomeType` ⇒ `UNCLASSIFIED` ⇒ bootstrap BLOCKED
  （不随 Binance 新增枚举自动忽略）；
- **去重键 `(income_type, tran_id)`**：同 key 内容不同 ⇒ `IncomeHistoryConflictError`（HISTORY_CONFLICT ⇒ BLOCKED）；
- **资产契约**：参与交易 PnL 的行若 `asset != USDT` ⇒ `UnsupportedIncomeAssetError`（不换算、不忽略）；
- 本模块**不**制造 synthetic Fill / Funding，也不写任何账本；
- 本模块只产出 **Binance 侧事实**（`IncomeHistoryFacts`）；把它转成领域契约
  `HistoricalRiskBaseline` 的映射放在 `readiness/evidence.py`（那里本来就可以同时依赖 connectors 与 risk），
  以保持 private 层不依赖 risk 领域（架构约束由 `tests/unit/test_private_isolation.py` 固定）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from market.events.types import Milliseconds

from connectors.binance.market_data.parsing import (
    require_decimal,
    require_field,
    require_int,
    require_mapping,
    require_str,
)
from connectors.binance.private.errors import translate_market_errors, PrivateFormatError
from connectors.binance.private.rest import PrivateRestClient

#: 交易所历史能力上限（官方：仅保留最近约 3 个月）。
HISTORY_WINDOW_LIMIT_MS = 90 * 86_400_000
#: 单页上限（官方 limit 最大值）。
MAX_PAGE_LIMIT = 1_000
#: 结算资产（本阶段 one-way / USDT-M）。
SETTLEMENT_ASSET = "USDT"


class IncomeClass(Enum):
    """income 行的性质分类。"""

    #: 交易产生的已实现盈亏 / 费用 / 资金费（计入账户级 daily trading PnL）
    TRADING = "trading"
    #: 纯钱包/资产划转（记录但**不**计入 trading PnL）
    NON_TRADING = "non_trading"
    #: 未识别 ⇒ bootstrap 必须 BLOCKED（未知 ≠ 0）
    UNCLASSIFIED = "unclassified"


#: 提案 §3 明确列出的交易类 incomeType。
TRADING_INCOME_TYPES = frozenset(
    {
        "REALIZED_PNL",
        "COMMISSION",
        "FUNDING_FEE",
        "SPECIAL_FUNDING_FEE",
    }
)

#: **明确**的非交易类型：目前只有 `TRANSFER`（人类裁决 2026-09-28）。
#:
#: 这里定义的不是"钱包余额变化"，而是 **可用于 RiskGate 的账户级 daily trading net realized**：
#: 任何其他类型（返佣 `FEE_RETURN`/`REFERRAL_KICKBACK`、清算 `INSURANCE_CLEAR`、交割结算、
#: `OPTIONS_*`、`WELCOME_BONUS`、跨保证金划转、以及未来新增枚举）都**不靠名字猜测**，
#: 一律 `UNCLASSIFIED` ⇒ BLOCKED，等真实账户出现对应类型后再做 evidence-driven 裁决。
NON_TRADING_INCOME_TYPES = frozenset({"TRANSFER"})


class IncomeHistoryError(PrivateFormatError):
    """income 历史事实不满足契约。"""


class IncomeHistoryConflictError(IncomeHistoryError):
    """同一 `(income_type, tran_id)` 出现内容不同的记录（HISTORY_CONFLICT ⇒ BLOCKED）。"""


class UnsupportedIncomeAssetError(IncomeHistoryError):
    """参与交易 PnL 的记录使用了非结算资产（UNSUPPORTED_INCOME_ASSET ⇒ BLOCKED）。"""


def classify_income_type(income_type: str) -> IncomeClass:
    """显式分类；未识别即 `UNCLASSIFIED`（绝不静默当 0）。"""
    if income_type in TRADING_INCOME_TYPES:
        return IncomeClass.TRADING
    if income_type in NON_TRADING_INCOME_TYPES:
        return IncomeClass.NON_TRADING
    return IncomeClass.UNCLASSIFIED


def _audit_identity(row: "IncomeRow") -> tuple[object, ...]:
    """non-trading / unclassified 行的**审计身份**（完整 canonical row fingerprint）。

    这些行不参与 `daily_net_realized`，因此 `tranId` 的"唯一性"不能当作业务身份
    （真实数据：`TRANSFER` 的 `tranId` 恒为 0）。审计层用完整行内容去重即可。
    """
    return (
        row.income_type,
        row.income,
        row.asset,
        row.time_ms,
        row.tran_id,
        row.trade_id,
        row.symbol,
        row.info,
    )


@dataclass(frozen=True, slots=True)
class IncomeRow:
    """一条 income 事实（严格解析后的不可变记录）。"""

    income_type: str
    income: float
    asset: str
    time_ms: Milliseconds
    tran_id: int
    trade_id: str | None
    symbol: str | None
    info: str | None

    @property
    def classification(self) -> IncomeClass:
        return classify_income_type(self.income_type)

    @property
    def is_trading(self) -> bool:
        return self.classification is IncomeClass.TRADING

    @property
    def key(self) -> tuple[str, int]:
        """去重键（提案 §6）：`(income_type, tran_id)`。"""
        return (self.income_type, self.tran_id)


@dataclass(frozen=True, slots=True)
class IncomeHistoryCoverage:
    """分页覆盖情况（`complete=False` ⇒ 不允许当作已知）。"""

    start_ts: Milliseconds
    end_ts: Milliseconds
    rows: int
    pages: int
    complete: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class IncomeHistoryFacts:
    """归一化后的账户收入历史事实。"""

    rows: tuple[IncomeRow, ...]
    coverage: IncomeHistoryCoverage
    duplicates_dropped: int

    def by_class(self, classification: IncomeClass) -> tuple[IncomeRow, ...]:
        return tuple(row for row in self.rows if row.classification is classification)

    @property
    def trading_rows(self) -> tuple[IncomeRow, ...]:
        return self.by_class(IncomeClass.TRADING)

    @property
    def non_trading_rows(self) -> tuple[IncomeRow, ...]:
        return self.by_class(IncomeClass.NON_TRADING)

    @property
    def unclassified_rows(self) -> tuple[IncomeRow, ...]:
        return self.by_class(IncomeClass.UNCLASSIFIED)

    @property
    def trading_net_realized(self) -> float:
        """交易类现金流求和（Binance 的 `income` 已带符号：直接相加，不再人工取负）。"""
        return sum(row.income for row in self.trading_rows)


def _optional_text(value: object, *, path: str) -> str | None:
    """可选文本字段：缺失 / 空字符串 → `None`（**真实 payload 证据**）。

    2026-09-28 实测：`TRANSFER` 行的 `symbol` / `tradeId` / `info` 都是 `""`（不是缺字段），
    因此把空串视为"无值"；类型不对仍然 fail closed。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise IncomeHistoryError(f"{path}: expected a string, got {type(value).__name__}")
    return value or None


@translate_market_errors
def parse_income_rows(raw: object) -> tuple[IncomeRow, ...]:
    """严格解析 `/fapi/v1/income` 的一页响应。"""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise IncomeHistoryError("income response must be an array")
    rows: list[IncomeRow] = []
    for entry in raw:
        item = require_mapping(entry, path="income[]")
        income_type = require_str(require_field(item, "incomeType", path="income[]"), path="income[].incomeType")
        time_ms = require_int(require_field(item, "time", path="income[]"), path="income[].time")
        rows.append(
            IncomeRow(
                income_type=income_type,
                income=require_decimal(require_field(item, "income", path="income[]"), path="income[].income"),
                asset=require_str(require_field(item, "asset", path="income[]"), path="income[].asset"),
                time_ms=time_ms,
                tran_id=require_int(require_field(item, "tranId", path="income[]"), path="income[].tranId"),
                trade_id=_optional_text(item.get("tradeId"), path="income[].tradeId"),
                symbol=_optional_text(item.get("symbol"), path="income[].symbol"),
                info=_optional_text(item.get("info"), path="income[].info"),
            )
        )
    return tuple(rows)


def dedupe_income_rows(
    rows: Sequence[IncomeRow],
) -> tuple[tuple[IncomeRow, ...], int]:
    """去重（人类裁决 2026-09-28：**审计身份问题不得污染风险计算**）。

    规则按"是否参与 daily PnL"分开：

    | 行类型 | 身份 | 冲突语义 |
    | --- | --- | --- |
    | TRADING（PnL 贡献） | `(income_type, tran_id)` | 同 key 内容不同 ⇒ `HISTORY_CONFLICT`（**保持严格**） |
    | NON_TRADING / UNCLASSIFIED | 完整行内容（审计层） | 完全相同 ⇒ 去重计数；不同内容 ⇒ **合法共存**（不 BLOCKED） |

    幂等：重复分页 / 重试 / 重跑 bootstrap 都必须得到同一结果。
    """
    trading: dict[tuple[str, int], IncomeRow] = {}
    audit: set[tuple[object, ...]] = set()
    ordered: list[IncomeRow] = []
    dropped = 0
    for row in rows:
        if row.is_trading:
            previous = trading.get(row.key)
            if previous is None:
                trading[row.key] = row
                ordered.append(row)
                continue
            if previous == row:
                dropped += 1
                continue
            raise IncomeHistoryConflictError(
                f"HISTORY_CONFLICT: {row.key[0]}/{row.key[1]} appears twice with different content "
                f"({previous.income}@{previous.time_ms} vs {row.income}@{row.time_ms})"
            )
        identity = _audit_identity(row)
        if identity in audit:
            dropped += 1
            continue
        audit.add(identity)
        ordered.append(row)
    return tuple(ordered), dropped


def verify_settlement_asset(rows: Sequence[IncomeRow]) -> None:
    """SC-8：**参与交易 PnL** 的行必须使用结算资产；非交易资金流只记录不参与。"""
    for row in rows:
        if row.classification is IncomeClass.TRADING and row.asset != SETTLEMENT_ASSET:
            raise UnsupportedIncomeAssetError(
                f"UNSUPPORTED_INCOME_ASSET: {row.income_type} uses {row.asset!r}, "
                f"only {SETTLEMENT_ASSET} is supported (no conversion, no silent drop)"
            )


def fetch_income_history(
    rest: PrivateRestClient,
    *,
    window_start_ms: Milliseconds,
    cutoff_ms: Milliseconds,
    page_limit: int = MAX_PAGE_LIMIT,
    max_pages: int,
) -> IncomeHistoryFacts:
    """完整读取 `[window_start_ms, cutoff_ms]` 的账户收入历史（只读、分页到底）。

    `max_pages` 必须由调用方显式给出：它是"读到多少页还没读完就放弃"的安全上限，
    超过即视为 **coverage 不完整**（不抛异常、不返回部分结果当作完整）。
    """
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise IncomeHistoryError("max_pages must be an int >= 1")
    if isinstance(page_limit, bool) or not isinstance(page_limit, int) or not 1 <= page_limit <= MAX_PAGE_LIMIT:
        raise IncomeHistoryError(f"page_limit must be in [1, {MAX_PAGE_LIMIT}]")
    if window_start_ms > cutoff_ms:
        raise IncomeHistoryError("window_start_ms must be <= cutoff_ms")

    collected: list[IncomeRow] = []
    pages = 0
    complete = False
    reason = ""
    for page in range(1, max_pages + 1):
        raw = rest.income_history(
            start_time=window_start_ms, end_time=cutoff_ms, page=page, limit=page_limit
        )
        page_rows = parse_income_rows(raw)
        pages = page
        collected.extend(page_rows)
        if len(page_rows) < page_limit:
            complete = True
            break
    else:
        reason = f"exceeded max_pages={max_pages} without reaching a short page"

    deduped, dropped = dedupe_income_rows(collected)
    verify_settlement_asset(deduped)
    out_of_window = tuple(row for row in deduped if row.time_ms > cutoff_ms)
    if out_of_window:
        complete = False
        reason = reason or "response contained rows after cutoff_ts"
    times = [row.time_ms for row in deduped]
    coverage = IncomeHistoryCoverage(
        start_ts=min(times) if times else window_start_ms,
        end_ts=max(times) if times else cutoff_ms,
        rows=len(deduped),
        pages=pages,
        complete=complete,
        reason=reason,
    )
    return IncomeHistoryFacts(rows=deduped, coverage=coverage, duplicates_dropped=dropped)


__all__ = [
    "HISTORY_WINDOW_LIMIT_MS",
    "MAX_PAGE_LIMIT",
    "NON_TRADING_INCOME_TYPES",
    "SETTLEMENT_ASSET",
    "TRADING_INCOME_TYPES",
    "IncomeClass",
    "IncomeHistoryConflictError",
    "IncomeHistoryCoverage",
    "IncomeHistoryError",
    "IncomeHistoryFacts",
    "IncomeRow",
    "UnsupportedIncomeAssetError",
    "classify_income_type",
    "dedupe_income_rows",
    "fetch_income_history",
    "parse_income_rows",
    "verify_settlement_asset",
]
