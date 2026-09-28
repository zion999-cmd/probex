"""RiskSnapshot 构建：把 mutable accounting 事实转成不可变快照（§8 / §12）。

- Risk 不直接到处读取 mutable accounting 对象；`RiskGate` 只消费 `RiskSnapshot`。
- `now_ms` 与 `day_start_ts` 由**调用方注入**（通常来自注入的 Clock）；本模块不使用 wall-clock。
- 未知 ≠ 0：缺 mark / 缺当日盈亏来源时字段为 `None`，由 gate fail closed。
"""

from __future__ import annotations

import math

from market.events.types import Milliseconds
from portfolio.accounting import AccountingCore
from portfolio.types import LiquidationInfo
from risk.high_watermark import HighWatermarkEvidence
from risk.history import HistoricalRiskBaseline
from risk.types import AvailableBalanceSource, ExchangeAvailableBalance, RiskSnapshot

#: 一天的毫秒数（用于 UTC 日界）。
_DAY_MS = 86_400_000


def utc_day_start_ms(now_ms: Milliseconds) -> Milliseconds:
    """给定时刻所在的 UTC 日零点（Daily Loss 的默认边界）。

    只是 helper：调用方可以传入任意 `day_start_ts`（例如交易所结算时间）。
    """
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative int epoch-millisecond value")
    return (now_ms // _DAY_MS) * _DAY_MS


def build_risk_snapshot(
    accounting: AccountingCore,
    *,
    symbol: str,
    now_ms: Milliseconds,
    open_order_exposure: float = 0.0,
    confirmed_open_exposure: float | None = None,
    uncertain_exposure: float = 0.0,
    unresolved_order_count: int = 0,
    liquidation: LiquidationInfo | None = None,
    day_start_ts: Milliseconds | None = None,
    exchange_available_balance: ExchangeAvailableBalance | None = None,
    historical_baseline: HistoricalRiskBaseline | None = None,
    high_watermark: HighWatermarkEvidence | None = None,
) -> RiskSnapshot:
    """构建某个 symbol 的风险快照。

    `open_order_exposure` 是**总量**（= confirmed + uncertain）。允许只给总量（此时 confirmed 由减法推出）；
    若三个值都给且不自洽，直接 `ValueError`（fail closed，避免调用方给出矛盾的暴露）。

    `exchange_available_balance`（P0001.9.4 §3）：传入时 `available_balance` 使用**交易所事实**
    （Binance `availableBalance` + 采集时刻），否则保持 P0001.5 的本地推导
    （`balance - open_order_exposure`）——PAPER / REPLAY 语义**不变**。

    `historical_baseline`（P0001.9.4.1 §9）：传入**受信**的历史 baseline 时，
    `realized_pnl_today = baseline.daily_net_realized + local_net_realized_since(cutoff)`；
    缺任一段 ⇒ `None`（未知 ≠ 0）。**不**使用 baseline 推导 peak/drawdown（本阶段永远未知）。
    不传时完全沿用原来的 `day_start_ts` 本地路径（Paper / Replay 不变）。

    `high_watermark`（P0001.9.4.2 §15）：传入**已确认**的 durable HWM 证据时，
    `peak_equity` / `drawdown` / `drawdown_pct` 由 "自 activation 起的峰值 vs 当前 equity" 计算；
    HWM 非 ACTIVE 或未提供时保持未知（`None`）⇒ 已配置限额时 `RiskGate: MISSING_DRAWDOWN`（fail closed）。
    **Paper / Replay 路径不受影响**（不传该参数即完全保持原语义）。
    """
    _validate_inputs(
        symbol=symbol,
        now_ms=now_ms,
        open_order_exposure=open_order_exposure,
        liquidation=liquidation,
        day_start_ts=day_start_ts,
    )
    confirmed, uncertain, unresolved = _resolve_exposure_inputs(
        total=open_order_exposure,
        confirmed=confirmed_open_exposure,
        uncertain=uncertain_exposure,
        unresolved_order_count=unresolved_order_count,
    )
    return _assemble_snapshot(
        accounting,
        symbol=symbol,
        now_ms=now_ms,
        exposed_total=float(open_order_exposure),
        confirmed=confirmed,
        uncertain=uncertain,
        unresolved=unresolved,
        liquidation=liquidation,
        day_start_ts=day_start_ts,
        exchange_available_balance=exchange_available_balance,
        historical_baseline=historical_baseline,
        high_watermark=high_watermark,
    )


def _assemble_snapshot(
    accounting: AccountingCore,
    *,
    symbol: str,
    now_ms: Milliseconds,
    exposed_total: float,
    confirmed: float,
    uncertain: float,
    unresolved: int,
    liquidation: LiquidationInfo | None,
    day_start_ts: Milliseconds | None,
    exchange_available_balance: ExchangeAvailableBalance | None = None,
    historical_baseline: HistoricalRiskBaseline | None = None,
    high_watermark: HighWatermarkEvidence | None = None,
) -> RiskSnapshot:
    """把 mutable accounting 事实转成不可变快照（字段逐个映射，不做判断）。"""
    mark_price = accounting.mark_price(symbol)
    mark_timestamp = accounting.mark_timestamp(symbol)
    mark_age_ms = None if mark_price is None or mark_timestamp is None else max(0, now_ms - mark_timestamp)
    position = accounting.position(symbol)
    drawdown, drawdown_pct, peak_equity = _derive_drawdown(accounting)
    if high_watermark is not None:
        # P0001.9.4.2：Live 路径使用 durable HWM（自 activation 起）覆盖 session 峰值语义
        drawdown, drawdown_pct, peak_equity = _derive_durable_drawdown(
            accounting, high_watermark=high_watermark
        )

    return RiskSnapshot(
        symbol=symbol,
        now_ms=now_ms,
        balance=accounting.balance,
        equity=accounting.equity(),
        position_qty=position.qty,
        position_notional=position.notional,
        gross_exposure=accounting.gross_exposure,
        net_exposure=accounting.net_exposure,
        open_order_exposure=exposed_total,
        available_balance=(
            accounting.balance - exposed_total
            if exchange_available_balance is None
            else exchange_available_balance.value
        ),
        realized_pnl_today=_realized_pnl_today(
            accounting, day_start_ts=day_start_ts, historical_baseline=historical_baseline
        ),
        unrealized_pnl=accounting.unrealized_pnl(),
        drawdown=drawdown,
        mark_price=mark_price,
        peak_equity=peak_equity,
        drawdown_pct=drawdown_pct,
        mark_age_ms=mark_age_ms,
        liquidation=liquidation,
        trading_fees=accounting.trading_fees,
        funding=accounting.funding_total,
        net_realized=accounting.net_realized,
        confirmed_open_exposure=confirmed,
        uncertain_exposure=uncertain,
        unresolved_order_count=unresolved,
        available_balance_source=(
            AvailableBalanceSource.LOCAL_DERIVED
            if exchange_available_balance is None
            else exchange_available_balance.source
        ),
        available_balance_captured_at=(
            None if exchange_available_balance is None else exchange_available_balance.captured_at
        ),
        available_balance_age_ms=(
            None if exchange_available_balance is None else exchange_available_balance.age_ms(now_ms=now_ms)
        ),
    )


def _resolve_exposure_inputs(
    *,
    total: float,
    confirmed: float | None,
    uncertain: float,
    unresolved_order_count: int,
) -> tuple[float, float, int]:
    """校验并归一化暴露入参（不自洽即 fail closed）。"""
    resolved_confirmed = _resolve_confirmed_exposure(
        total=float(total), confirmed=confirmed, uncertain=float(uncertain)
    )
    if (
        isinstance(unresolved_order_count, bool)
        or not isinstance(unresolved_order_count, int)
        or unresolved_order_count < 0
    ):
        raise ValueError("unresolved_order_count must be a non-negative int")
    return resolved_confirmed, float(uncertain), unresolved_order_count


def _resolve_confirmed_exposure(
    *, total: float, confirmed: float | None, uncertain: float
) -> float:
    """推导并校验 `confirmed + uncertain == total`（不自洽则 fail closed）。"""
    if uncertain < 0.0 or not math.isfinite(uncertain):
        raise ValueError("uncertain_exposure must be a non-negative finite number")
    if confirmed is None:
        return total - uncertain
    if confirmed < 0.0 or not math.isfinite(confirmed):
        raise ValueError("confirmed_open_exposure must be a non-negative finite number")
    if abs(total - (confirmed + uncertain)) > 1e-9:
        raise ValueError(
            f"open_order_exposure {total} is inconsistent with "
            f"confirmed {confirmed} + uncertain {uncertain}"
        )
    return confirmed


def _validate_inputs(
    *,
    symbol: str,
    now_ms: Milliseconds,
    open_order_exposure: float,
    liquidation: LiquidationInfo | None,
    day_start_ts: Milliseconds | None,
) -> None:
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be a non-empty string")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative int epoch-millisecond value")
    if (
        isinstance(open_order_exposure, bool)
        or not isinstance(open_order_exposure, (int, float))
        or not math.isfinite(float(open_order_exposure))
        or float(open_order_exposure) < 0.0
    ):
        raise ValueError("open_order_exposure must be a non-negative finite number")
    if liquidation is not None and liquidation.symbol != symbol:
        raise ValueError(f"liquidation info is for {liquidation.symbol!r}, not {symbol!r}")
    if day_start_ts is not None and (
        isinstance(day_start_ts, bool) or not isinstance(day_start_ts, int) or day_start_ts < 0
    ):
        raise ValueError("day_start_ts must be a non-negative int epoch-millisecond value or None")


def _realized_pnl_today(
    accounting: AccountingCore,
    *,
    day_start_ts: Milliseconds | None,
    historical_baseline: HistoricalRiskBaseline | None,
) -> float | None:
    """当日已实现盈亏：优先使用受信历史 baseline（Live），否则沿用本地账本（Paper / Replay）。"""
    if historical_baseline is None:
        return None if day_start_ts is None else accounting.net_realized_since(day_start_ts)
    return historical_baseline.compose_daily_pnl(accounting.net_realized_since(historical_baseline.cutoff_ts))


def _derive_durable_drawdown(
    accounting: AccountingCore, *, high_watermark: HighWatermarkEvidence
) -> tuple[float | None, float | None, float | None]:
    """用 durable HWM 计算 (drawdown, drawdown_pct, peak_equity)。

    - HWM 非 ACTIVE / equity 未知 ⇒ 三者皆 `None`（未知 ≠ 0 ⇒ RiskGate fail closed）；
    - 否则 `drawdown = max(0, peak - equity)`、`drawdown_pct = drawdown / peak`。
    """
    equity = accounting.equity()
    observation = high_watermark.equity_observation()
    if equity is None or observation is None:
        return None, None, None
    peak_equity, _peak_ts = observation
    drawdown = max(0.0, peak_equity - equity)
    drawdown_pct = 0.0 if peak_equity <= 0.0 else drawdown / peak_equity
    return drawdown, drawdown_pct, peak_equity


def _derive_drawdown(accounting: AccountingCore) -> tuple[float | None, float | None, float | None]:
    """返回 (drawdown, drawdown_pct, peak_equity)。

    P0001.9.4 §2（修正事实链）：**历史峰值未知时不得用当前 equity 冒充峰值**。
    `peak_equity is None`（例如 startup baseline 之后 `historical_pnl_known = False`）⇒
    三元组全为 `None` ⇒ `RiskGate` 以 `MISSING_DRAWDOWN` fail closed。
    禁止为了让 readiness 变绿而把启动时 equity 定义成历史 peak。
    """
    equity = accounting.equity()
    if equity is None:
        return None, None, None

    peak_equity = accounting.peak_equity
    if peak_equity is None:
        # 未知 ≠ 0：历史峰值未知就是未知，不制造 drawdown = 0 的假事实
        return None, None, None

    drawdown = max(0.0, peak_equity - equity)
    drawdown_pct = 0.0 if peak_equity <= 0.0 else drawdown / peak_equity
    return drawdown, drawdown_pct, peak_equity


__all__ = ["build_risk_snapshot", "utc_day_start_ms"]
