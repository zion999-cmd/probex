"""Accounting Core：账户状态的唯一 Owner。

```text
Fill / Funding / Mark ──▶ AccountingCore ──▶ Position / Balance / Realized / Unrealized / Equity / Exposure
```

纪律（P0001.5）：

- `Fill` 是唯一成交事实源；Position / PnL / Balance 全部由账本派生。
- **Balance ≠ Equity**：`balance` 是已结算现金，`equity = balance + Σ unrealized`。
- **Realized ≠ Fee ≠ Funding**：`realized_pnl` 只含交易盈亏；费用累计到 `trading_fees`；
  资金费累计到 `funding`。`net_realized = realized_trade_pnl − trading_fees + funding`。
- **Mark price 只影响 unrealized / equity**，绝不改变 realized。
- **未知 ≠ 0**：非 flat 且无 mark 时，`unrealized_pnl` / `equity` 为 `None`（调用方必须 fail closed）。
- 一切时间戳来自事件或调用方注入，本模块不使用 wall-clock。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from market.events.types import Milliseconds
from portfolio.fills import FillLedger, FillOutcome
from portfolio.funding import FundingLedger
from portfolio.position import Position, apply_fill
from portfolio.types import (
    ExternalAccountBaseline,
    InvalidBaselineError,
    PortfolioError,
    Fill,
    FundingPayment,
    UnsupportedAssetError,
)


@dataclass(frozen=True, slots=True)
class FillApplication:
    """一次 `record_fill` 的结果（重复成交也返回结果，但 realized/fee 均为 0）。"""

    outcome: FillOutcome
    fill: Fill
    position: Position
    realized_delta: float
    trading_fee_applied: float
    closed_qty: float
    opened_qty: float
    is_reversal: bool

    @property
    def accepted(self) -> bool:
        return self.outcome.accepted


class BaselineAlreadyAppliedError(PortfolioError):
    """已 bootstrap 过（或在有 session Fill 之后）再次 bootstrap。"""


@dataclass(frozen=True, slots=True)
class BaselineApplication:
    """一次 startup baseline 的应用结果（审计用）。"""

    baseline: ExternalAccountBaseline
    position: Position
    balance: float
    equity: float | None
    historical_pnl_known: bool


class AccountingCore:
    """单一账户的账本与持仓状态。"""

    def __init__(self, *, settlement_asset: str = "USDT", initial_balance: float = 0.0) -> None:
        if not isinstance(settlement_asset, str) or not settlement_asset:
            raise ValueError("settlement_asset must be a non-empty string")
        if isinstance(initial_balance, bool) or not isinstance(initial_balance, (int, float)):
            raise ValueError("initial_balance must be a number")
        if not math.isfinite(float(initial_balance)):
            raise ValueError("initial_balance must be finite")

        self._settlement_asset = settlement_asset
        self._initial_balance = float(initial_balance)
        self._fills = FillLedger()
        self._funding = FundingLedger(settlement_asset=settlement_asset)
        self._positions: dict[str, Position] = {}
        self._marks: dict[str, float] = {}
        self._mark_timestamps: dict[str, Milliseconds] = {}
        self._realized_events: list[tuple[Milliseconds, float, float]] = []  # (ts, realized_delta, fee)
        self._peak_equity = float("-inf")
        #: startup baseline（P0001.9.3）：一次性、只在尚无本 session Fill 时允许
        self._baseline: ExternalAccountBaseline | None = None
        self._baseline_balance_offset = 0.0
        #: 历史 PnL / 历史峰值是否已知（baseline 之后为 False ⇒ 相关指标返回 None，由 RiskGate fail closed）
        self._historical_pnl_known = True

    # ------------------------------------------------------------------ 入账

    def record_fill(self, fill: Fill) -> FillApplication:
        """记录一笔成交并更新持仓。重复成交不改变任何状态。"""
        if fill.fee_asset != self._settlement_asset:
            raise UnsupportedAssetError(
                f"fill fee asset {fill.fee_asset!r} != settlement asset {self._settlement_asset!r}; "
                "多币种换算不在本阶段范围内"
            )

        outcome = self._fills.record(fill)
        position = self._positions.get(fill.symbol, Position(symbol=fill.symbol))
        if not outcome.accepted:
            return FillApplication(
                outcome=outcome,
                fill=fill,
                position=position,
                realized_delta=0.0,
                trading_fee_applied=0.0,
                closed_qty=0.0,
                opened_qty=0.0,
                is_reversal=False,
            )

        update = apply_fill(position, fill)
        marked = self._with_mark(update.position)
        self._positions[fill.symbol] = marked
        self._realized_events.append((fill.exchange_ts, update.realized_delta, fill.fee))
        self._refresh_peak_equity()

        return FillApplication(
            outcome=outcome,
            fill=fill,
            position=marked,
            realized_delta=update.realized_delta,
            trading_fee_applied=fill.fee,
            closed_qty=update.closed_qty,
            opened_qty=update.opened_qty,
            is_reversal=update.is_reversal,
        )

    def record_funding(self, payment: FundingPayment) -> None:
        """资金费独立入账（影响 balance / equity，不影响 realized trade pnl）。"""
        self._funding.record(payment)
        self._refresh_peak_equity()

    def update_mark_price(self, symbol: str, price: float, *, timestamp: Milliseconds) -> None:
        """显式注入 mark price（不使用 last trade）。"""
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ValueError("price must be a number")
        mark = float(price)
        if not math.isfinite(mark) or mark <= 0.0:
            raise ValueError(f"mark price must be positive and finite, got {price!r}")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("timestamp must be a non-negative int epoch-millisecond value")

        self._marks[symbol] = mark
        self._mark_timestamps[symbol] = timestamp
        position = self._positions.get(symbol)
        if position is not None:
            self._positions[symbol] = position.with_mark(mark)
        self._refresh_peak_equity()

    def bootstrap_from_baseline(self, baseline: ExternalAccountBaseline) -> BaselineApplication:
        """用交易所账户事实**一次性** bootstrap 本地账本（不产生 synthetic Fill）。

        拒绝条件（fail closed）：

        - 已经 bootstrap 过（禁止第二次偷偷覆盖 Accounting 状态）；
        - 本 session 已经处理过任何 Fill（说明本地历史已经存在，baseline 会掩盖它）。
        """
        if not isinstance(baseline, ExternalAccountBaseline):
            raise InvalidBaselineError("baseline must be an ExternalAccountBaseline")
        if self._baseline is not None:
            raise BaselineAlreadyAppliedError(
                "accounting already bootstrapped from a startup baseline; "
                "subsequent changes must come from real fills/funding/mark"
            )
        if len(self._fills.fills) > 0:
            raise BaselineAlreadyAppliedError(
                "accounting has already processed session fills; refusing to overwrite state with a baseline"
            )
        if baseline.wallet_balance < 0.0 or baseline.available_balance < 0.0:
            raise InvalidBaselineError("baseline balances must be non-negative")

        position = Position(
            symbol=baseline.symbol,
            qty=baseline.position_qty,
            avg_entry_price=baseline.entry_price if baseline.position_qty != 0.0 else 0.0,
            mark_price=baseline.mark_price if baseline.mark_price > 0.0 else None,
        )
        self._baseline = baseline
        self._positions[baseline.symbol] = position
        if baseline.mark_price > 0.0:
            self._marks[baseline.symbol] = baseline.mark_price
            self._mark_timestamps[baseline.symbol] = baseline.captured_at
        # balance 由账本派生（D-020）⇒ 用 offset 让 balance 等于交易所 wallet balance，且之后只随真实流水变化
        self._baseline_balance_offset = baseline.wallet_balance - (self._initial_balance + self.net_realized)
        # 历史峰值/PnL 未知：本 session 不再做 drawdown 判定（RiskGate 因此 fail closed）
        self._historical_pnl_known = False

        return BaselineApplication(
            baseline=baseline,
            position=position,
            balance=self.balance,
            equity=self.equity(),
            historical_pnl_known=False,
        )

    # ------------------------------------------------------------------ 只读事实

    @property
    def settlement_asset(self) -> str:
        return self._settlement_asset

    @property
    def fills(self) -> FillLedger:
        return self._fills

    @property
    def funding(self) -> FundingLedger:
        return self._funding

    def position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position(symbol=symbol))

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def mark_price(self, symbol: str) -> float | None:
        return self._marks.get(symbol)

    def mark_timestamp(self, symbol: str) -> Milliseconds | None:
        return self._mark_timestamps.get(symbol)

    @property
    def initial_balance(self) -> float:
        return self._initial_balance

    @property
    def baseline(self) -> ExternalAccountBaseline | None:
        """已应用的 startup baseline；未 bootstrap 时为 None。"""
        return self._baseline

    @property
    def baseline_applied(self) -> bool:
        return self._baseline is not None

    @property
    def historical_pnl_known(self) -> bool:
        """历史 PnL / 历史峰值是否可信（baseline 之后为 False）。"""
        return self._historical_pnl_known

    @property
    def realized_trade_pnl(self) -> float:
        """累计已实现**交易**盈亏（不含手续费、不含资金费）。"""
        return float(sum(position.realized_pnl for position in self._positions.values()))

    @property
    def trading_fees(self) -> float:
        """累计交易手续费（独立于 realized / funding）。"""
        return float(sum(fee for _ts, _delta, fee in self._realized_events))

    @property
    def funding_total(self) -> float:
        return self._funding.total

    @property
    def net_realized(self) -> float:
        """`realized_trade_pnl − trading_fees + funding`。"""
        return self.realized_trade_pnl - self.trading_fees + self.funding_total

    @property
    def balance(self) -> float:
        """已结算现金（Balance ≠ Equity）。

        `baseline` 之后等于交易所 wallet balance，并且此后**只**随真实 Fill / Funding 变化（D-020 账本派生 + offset）。
        """
        return self._initial_balance + self._baseline_balance_offset + self.net_realized

    def unrealized_pnl(self) -> float | None:
        """Σ 未实现盈亏；任一非 flat 持仓缺 mark 时为 `None`。"""
        total = 0.0
        for position in self._positions.values():
            value = position.unrealized_pnl
            if value is None:
                return None
            total += value
        return float(total)

    def equity(self) -> float | None:
        """`balance + Σ unrealized`；未实现未知时为 `None`。"""
        unrealized = self.unrealized_pnl()
        if unrealized is None:
            return None
        return self.balance + unrealized

    @property
    def peak_equity(self) -> float | None:
        """历史最高 equity（用于 drawdown）。

        `baseline` 之后**历史峰值未知** ⇒ 返回 `None`（不允许把「本 session 起点」冒充历史峰值，
        否则 drawdown 会长期显示为 0，看起来像真的）。
        """
        if not self._historical_pnl_known:
            return None
        return None if self._peak_equity == float("-inf") else self._peak_equity

    def drawdown(self) -> float | None:
        """`peak_equity − equity`（>= 0）；数据不足时为 `None`。"""
        equity = self.equity()
        peak = self.peak_equity
        if equity is None or peak is None:
            return None
        return max(0.0, peak - equity)

    @property
    def gross_exposure(self) -> float | None:
        """Σ|notional|；任一非 flat 持仓缺 mark 时为 `None`。"""
        total = 0.0
        for position in self._positions.values():
            notional = position.notional
            if notional is None:
                return None
            total += notional
        return float(total)

    @property
    def net_exposure(self) -> float | None:
        """|Σ signed notional|；任一非 flat 持仓缺 mark 时为 `None`。"""
        total = 0.0
        for position in self._positions.values():
            notional = position.notional
            if notional is None:
                return None
            total += notional if position.qty > 0.0 else -notional
        return abs(float(total))

    def realized_trade_pnl_since(self, timestamp: Milliseconds) -> float:
        return float(sum(delta for ts, delta, _fee in self._realized_events if ts >= timestamp))

    def trading_fees_since(self, timestamp: Milliseconds) -> float:
        return float(sum(fee for ts, _delta, fee in self._realized_events if ts >= timestamp))

    def funding_since(self, timestamp: Milliseconds) -> float:
        return self._funding.total_since(timestamp)

    def net_realized_since(self, timestamp: Milliseconds) -> float | None:
        """`realized − fees + funding`（用于 Daily Loss 判定）。

        `baseline` 之后，若查询窗口的起点**早于** baseline 捕获时刻，则返回 `None`（未知 ⇒ 调用方 fail closed）：
        baseline 只说明「现在是什么状态」，**不能**证明启动前赚亏多少。
        """
        if self._baseline is not None and timestamp < self._baseline.captured_at:
            return None
        return (
            self.realized_trade_pnl_since(timestamp)
            - self.trading_fees_since(timestamp)
            + self.funding_since(timestamp)
        )

    # ------------------------------------------------------------------ 内部

    def _with_mark(self, position: Position) -> Position:
        mark = self._marks.get(position.symbol)
        return position if mark is None else position.with_mark(mark)

    def _refresh_peak_equity(self) -> None:
        equity = self.equity()
        if equity is None:
            return
        if equity > self._peak_equity:
            self._peak_equity = equity


__all__ = ["AccountingCore", "BaselineAlreadyAppliedError", "BaselineApplication", "FillApplication"]
