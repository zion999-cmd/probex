"""Metric Contract（P0001.11 §3 / 裁决 A）：正式指标定义 + 纯计算。

每个指标都必须说明：**公式 / 时间基准 / 输入事实 Owner / UNKNOWN 条件 / 采样方式**。
计算只在**调用方提供事实**的前提下进行；报告层**不推导账户真相**
（Net/Realized/Unrealized PnL、Fees、Funding 一律由 AccountingCore 的结果传入，缺失即 UNKNOWN）。
UI 不得自行计算（只读 `GET /api/v1/metrics` 的定义与报告里的值）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, pstdev

from product.types import Fact

#: 第一版固定采样间隔（裁决 A：Sharpe 用 1 分钟采样）
SHARPE_SAMPLE_INTERVAL_MS = 60_000

#: Sharpe 最小样本数（裁决 A：< 30 ⇒ UNKNOWN）
SHARPE_MIN_SAMPLES = 30

#: 重采样上限（防止跨天区间生成海量边界；超出 ⇒ UNKNOWN，不静默截断）
MAX_RESAMPLED_SAMPLES = 20_000


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """指标契约（必须有公式与 UNKNOWN 语义）。"""

    name: str
    label: str
    formula: str
    time_base: str
    fact_owner: str
    unknown_condition: str
    sampling: str


@dataclass(frozen=True, slots=True)
class EquitySample:
    """一个**已知**的 equity 采样点（ts, equity）。"""

    ts: int
    equity: float


@dataclass(frozen=True, slots=True)
class RealizedTradeResult:
    """一笔**已实现**交易结果（只用于 Profit Factor；fees/funding 不混入）。"""

    realized_pnl: float


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """一次运行的指标集合（每个值都是 `Fact`：known 或带原因的 UNKNOWN）。"""

    values: dict[str, Fact]

    def fact(self, name: str) -> Fact:
        return self.values.get(name, Fact.unknown(f"unknown metric {name!r}"))

    def known_names(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, fact in self.values.items() if fact.known))


#: 指标定义表（顺序固定，供 API/UI/报告使用；**UI 不得重算**）
METRIC_DEFINITIONS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        name="net_pnl", label="Net PnL",
        formula="AccountingCore 提供的净结果（含 realized + unrealized − fees − funding）；报告层不重算",
        time_base="run 结束时刻（或最后一次已知账户事实）",
        fact_owner="AccountingCore",
        unknown_condition="AccountingCore 未提供该权威结果 ⇒ UNKNOWN（绝不由报告推导）",
        sampling="run 内最终值（非采样序列）",
    ),
    MetricDefinition(
        name="realized_pnl", label="Realized PnL",
        formula="AccountingCore.realized_trade_pnl（已实现交易盈亏，不含未实现）",
        time_base="run 结束时刻", fact_owner="AccountingCore",
        unknown_condition="未提供 ⇒ UNKNOWN", sampling="run 内最终值",
    ),
    MetricDefinition(
        name="unrealized_pnl", label="Unrealized PnL",
        formula="AccountingCore 未实现盈亏（mark 与入场价之差 × 持仓）",
        time_base="最后一次已知 mark/timestamp", fact_owner="AccountingCore",
        unknown_condition="未提供或 mark 未知 ⇒ UNKNOWN", sampling="run 内最终值",
    ),
    MetricDefinition(
        name="fees", label="Fees",
        formula="AccountingCore.trading_fees 累计",
        time_base="run 结束时刻", fact_owner="AccountingCore",
        unknown_condition="未提供 ⇒ UNKNOWN", sampling="run 内累计值",
    ),
    MetricDefinition(
        name="funding", label="Funding",
        formula="AccountingCore.funding 累计",
        time_base="run 结束时刻", fact_owner="AccountingCore",
        unknown_condition="未提供 ⇒ UNKNOWN", sampling="run 内累计值",
    ),
    MetricDefinition(
        name="max_confirmed_exposure", label="Max confirmed exposure",
        formula="max(OrderTracker.confirmed_open_exposure) —— 仅已确认挂单部分",
        time_base="run 内", fact_owner="OrderTracker",
        unknown_condition="未记录 ⇒ UNKNOWN", sampling="run 内采样最大值",
    ),
    MetricDefinition(
        name="max_total_exposure", label="Max total exposure",
        formula="max(OrderTracker.total_pending_exposure) —— **含 uncertain/pending**（不可丢弃）",
        time_base="run 内", fact_owner="OrderTracker",
        unknown_condition="未记录 ⇒ UNKNOWN", sampling="run 内采样最大值",
    ),
    MetricDefinition(
        name="run_mdd", label="Run MDD (run-internal)",
        formula="max over t of (peak_equity_before_t − equity_t) / peak_equity_before_t",
        time_base="**run 内** equity 序列（非 trusted activation point）",
        fact_owner="run equity samples（由调用方记录）",
        unknown_condition="样本 < 2 或任何 peak ≤ 0 ⇒ UNKNOWN",
        sampling="run 内 equity 采样（不重采样）",
    ),
    MetricDefinition(
        name="sharpe", label="Sharpe (per-period, not annualized)",
        formula="mean(r) / stdev(r)，r_i = equity_i / equity_{i-1} − 1；无风险利率 = 0；**未做年化**",
        time_base="固定 1 分钟采样网格", fact_owner="run equity samples（由调用方记录）",
        unknown_condition="有效样本 < 30、存在缺口、或 stdev(r) = 0 ⇒ UNKNOWN",
        sampling=f"固定间隔 {SHARPE_SAMPLE_INTERVAL_MS} ms（默认 1 分钟）",
    ),
    MetricDefinition(
        name="profit_factor", label="Profit Factor",
        formula="gross_profit / abs(gross_loss)（只含**已实现**交易结果；fees/funding 单列不混入）",
        time_base="run 内已实现交易序列", fact_owner="AccountingCore 的已实现交易结果",
        unknown_condition="无已实现交易、或**无亏损样本** ⇒ UNKNOWN（不返回 ∞）",
        sampling="逐笔已实现结果",
    ),
)

METRIC_NAMES: tuple[str, ...] = tuple(definition.name for definition in METRIC_DEFINITIONS)
OWNER_METRIC_NAMES: tuple[str, ...] = ("net_pnl", "realized_pnl", "unrealized_pnl", "fees", "funding")


class MetricError(ValueError):
    """指标计算契约错误（调用方传入非法事实）。"""


def metric_definition(name: str) -> MetricDefinition:
    for definition in METRIC_DEFINITIONS:
        if definition.name == name:
            return definition
    raise MetricError(f"unknown metric {name!r}")


def definitions_payload() -> tuple[dict[str, object], ...]:
    """定义表的产品形态（供 `GET /api/v1/metrics` 与 UI 展示；UI 据此显示公式，不重算）。"""
    return tuple(
        {
            "name": definition.name,
            "label": definition.label,
            "formula": definition.formula,
            "time_base": definition.time_base,
            "fact_owner": definition.fact_owner,
            "unknown_condition": definition.unknown_condition,
            "sampling": definition.sampling,
        }
        for definition in METRIC_DEFINITIONS
    )


def resample_equity(
    samples: Sequence[EquitySample],
    *,
    interval_ms: int = SHARPE_SAMPLE_INTERVAL_MS,
    max_samples: int = MAX_RESAMPLED_SAMPLES,
) -> tuple[tuple[EquitySample, ...], str | None]:
    """把稀疏 equity 采样重采样到固定网格（step function 语义，不插值）。

    返回 `(series, reason)`：`reason is None` 表示成功；否则 series 为空且 reason 说明原因。
    规则：只在**已知**采样点上取值（carry-forward 是合法的，因为 equity 是阶梯函数），
    首个网格点之前没有已知采样 ⇒ UNKNOWN（不猜）。
    """
    if interval_ms <= 0:
        raise MetricError("interval_ms must be > 0")
    if not samples:
        return (), "no equity samples recorded"
    ordered = sorted(samples, key=lambda item: item.ts)
    start, end = ordered[0].ts, ordered[-1].ts
    if end <= start:
        return (), "equity series has a single timestamp"
    count = (end - start) // interval_ms
    if count + 1 > max_samples:
        return (), f"resampled series would exceed {max_samples} samples"
    series: list[EquitySample] = []
    cursor = 0
    current: EquitySample | None = None
    boundary = start
    while boundary <= end:
        while cursor < len(ordered) and ordered[cursor].ts <= boundary:
            current = ordered[cursor]
            cursor += 1
        if current is None:
            return (), f"no known equity sample at or before grid point {boundary}"
        series.append(EquitySample(ts=boundary, equity=current.equity))
        boundary += interval_ms
    return tuple(series), None


def run_mdd(samples: Sequence[EquitySample]) -> Fact:
    """run 内 equity peak-to-trough（**不是** RiskGate 的 drawdown 权威）。"""
    if len(samples) < 2:
        return Fact.unknown("run MDD requires at least 2 equity samples")
    ordered = sorted(samples, key=lambda item: item.ts)
    peak = ordered[0].equity
    worst = 0.0
    for sample in ordered:
        peak = max(peak, sample.equity)
        if peak <= 0:
            return Fact.unknown("run MDD undefined: equity peak is not positive")
        worst = max(worst, (peak - sample.equity) / peak)
    return Fact.of(worst)


def sharpe(samples: Sequence[EquitySample], *,
           interval_ms: int = SHARPE_SAMPLE_INTERVAL_MS,
           min_samples: int = SHARPE_MIN_SAMPLES) -> Fact:
    """per-period Sharpe（**未年化**；rf = 0；样本 < min_samples ⇒ UNKNOWN）。"""
    series, reason = resample_equity(samples, interval_ms=interval_ms)
    if reason is not None:
        return Fact.unknown(reason)
    if len(series) < min_samples:
        return Fact.unknown(f"fewer than {min_samples} equity samples ({len(series)})")
    returns = [
        series[index].equity / series[index - 1].equity - 1.0
        for index in range(1, len(series))
        if series[index - 1].equity != 0.0
    ]
    if len(returns) < min_samples - 1:
        return Fact.unknown("not enough usable returns for Sharpe")
    deviation = pstdev(returns)
    if deviation == 0.0:
        return Fact.unknown("Sharpe undefined: return stdev is 0")
    return Fact.of(fmean(returns) / deviation)


def profit_factor(trades: Sequence[RealizedTradeResult]) -> Fact:
    """`gross_profit / abs(gross_loss)`（只含已实现；无亏损样本 ⇒ UNKNOWN）。"""
    if not trades:
        return Fact.unknown("no realized trades recorded")
    gross_profit = sum(trade.realized_pnl for trade in trades if trade.realized_pnl > 0)
    gross_loss = sum(trade.realized_pnl for trade in trades if trade.realized_pnl < 0)
    if gross_loss == 0.0:
        return Fact.unknown("profit factor undefined: no losing trades")
    return Fact.of(gross_profit / abs(gross_loss))


def compute_metrics(
    *,
    owner_facts: Mapping[str, float | None] | None = None,
    equity_samples: Sequence[EquitySample] = (),
    realized_trades: Sequence[RealizedTradeResult] = (),
    max_confirmed_exposure: float | None = None,
    max_total_exposure: float | None = None,
    interval_ms: int = SHARPE_SAMPLE_INTERVAL_MS,
    min_samples: int = SHARPE_MIN_SAMPLES,
) -> RunMetrics:
    """计算全部指标；每一格要么是 known，要么是**带原因的 UNKNOWN**。"""
    facts = dict(owner_facts or {})
    unknown_owner = "authoritative accounting fact not provided (reports never derive it)"
    values: dict[str, Fact] = {}
    for name in OWNER_METRIC_NAMES:
        values[name] = Fact.unknown(unknown_owner) if facts.get(name) is None else Fact.of(facts[name])
    values["max_confirmed_exposure"] = (
        Fact.unknown("max confirmed exposure was not recorded") if max_confirmed_exposure is None
        else Fact.of(max_confirmed_exposure)
    )
    values["max_total_exposure"] = (
        Fact.unknown("max total exposure was not recorded") if max_total_exposure is None
        else Fact.of(max_total_exposure)
    )
    values["run_mdd"] = run_mdd(equity_samples)
    values["sharpe"] = sharpe(equity_samples, interval_ms=interval_ms, min_samples=min_samples)
    values["profit_factor"] = profit_factor(realized_trades)
    for name in METRIC_NAMES:
        values.setdefault(name, Fact.unknown("metric not computed"))
    return RunMetrics(values={name: values[name] for name in METRIC_NAMES})


def metrics_payload(metrics: RunMetrics) -> dict[str, object]:
    """指标集合的产品形态（Fact → {known,value,reason}；UI 直接显示，不重算）。"""
    from product.serialization import to_jsonable

    return {name: to_jsonable(metrics.fact(name)) for name in METRIC_NAMES}


__all__ = [
    "MAX_RESAMPLED_SAMPLES",
    "METRIC_DEFINITIONS",
    "METRIC_NAMES",
    "OWNER_METRIC_NAMES",
    "SHARPE_MIN_SAMPLES",
    "SHARPE_SAMPLE_INTERVAL_MS",
    "EquitySample",
    "MetricDefinition",
    "MetricError",
    "RealizedTradeResult",
    "RunMetrics",
    "compute_metrics",
    "definitions_payload",
    "metric_definition",
    "metrics_payload",
    "profit_factor",
    "resample_equity",
    "run_mdd",
    "sharpe",
]
