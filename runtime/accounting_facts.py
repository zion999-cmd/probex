"""AccountingCore → 稳定只读事实（closure Slice 3 / Step 4，唯一 accounting owner 不变）。

设计（按人类要求）：

- **不猜内部形态**：只通过既有正式出口取值（`AccountingCore` 的公开属性/方法、以及
  `risk/snapshot.py::build_risk_snapshot` 这一既有契约）；不碰 `_private`；
- **只接收标量**：`equity` / `available_balance` 等若是方法就**显式调用**，
  只接受 `int/float/str/bool` 这类标量结果；callable、对象实例、内部方法引用一律不进入产品事实；
- **取不到就是 UNKNOWN + reason**，并且**绝不抛异常**（snapshot 不会因 accounting 崩）；
- 输出稳定结构，供 `ProductService` 直接映射（assembly 不再拼字段）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from product.account_timeline import AccountSample
from product.types import Fact

_SCALARS = (int, float, str, bool)


@dataclass(frozen=True, slots=True)
class AccountingFacts:
    """稳定只读结构（每个字段要么是具体标量，要么是带 reason 的 UNKNOWN）。"""

    equity: Fact
    available_balance: Fact
    realized_pnl: Fact
    unrealized_pnl: Fact
    #: F1：仓位语义三元组 —— known qty(>0) / known qty(==0, flat) / unknown
    position_known: Fact
    position_qty: Fact
    baseline_state: Fact
    last_update_at: Fact
    anomalies: tuple[str, ...] = ()

    def health(self) -> Fact:
        """accounting 健康度：equity 已知 ⇒ HEALTHY；未知 ⇒ UNKNOWN（绝不显示 HEALTHY）。"""
        return Fact.of("HEALTHY") if self.equity.known else Fact.unknown(
            "equity is unknown: " + str(self.equity.reason))


@dataclass(slots=True)
class AccountingFactsProvider:
    """把既有 accounting owner 的事实读成稳定结构（唯一 owner 仍是 AccountingCore）。"""

    accounting: object
    symbol: str
    clock: Callable[[], int]
    #: 可选：既有 OrderTracker 的暴露事实（total, confirmed）；不是新的 risk owner
    exposure_provider: Callable[[], tuple[float | None, float | None]] | None = None

    def facts(self) -> AccountingFacts:
        anomalies: list[str] = []
        equity = self._scalar(self.accounting, "equity", anomalies)
        balance = self._scalar(self.accounting, "balance", anomalies)
        realized = self._scalar(self.accounting, "realized_trade_pnl", anomalies)
        unrealized = self._scalar(self.accounting, "unrealized_pnl", anomalies)
        position = self._position(anomalies)
        position_known, position_qty = self._position_facts(position)
        baseline = self._scalar(self.accounting, "baseline_applied", anomalies)
        mark_ts = None
        try:
            mark_ts = self.accounting.mark_timestamp(self.symbol)      # 既有方法（显式调用）
        except Exception as exc:  # noqa: BLE001 - 取不到 ⇒ UNKNOWN（不抛）
            anomalies.append(f"mark_timestamp:{type(exc).__name__}")
        return AccountingFacts(
            equity=equity,
            available_balance=self._available_balance(anomalies, fallback=balance),
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            position_known=position_known,
            position_qty=position_qty,
            baseline_state=(Fact.unknown("baseline state unavailable") if baseline is None
                            else Fact.of("APPLIED" if baseline else "NOT_APPLIED")),
            last_update_at=Fact.of(None if mark_ts is None else int(mark_ts),
                                   unknown_reason="no mark timestamp recorded"),
            anomalies=tuple(anomalies))

    def sample(self, now_ms: int) -> AccountSample | None:
        """给 `BoundedAccountTimeline` 的采样（复用同一事实，不重算）。"""
        facts = self.facts()
        if not facts.equity.known and not facts.available_balance.known:
            return None
        total = confirmed = None
        if self.exposure_provider is not None:
            try:
                total, confirmed = self.exposure_provider()
            except Exception:  # noqa: BLE001 - 取不到就是 UNKNOWN
                total = confirmed = None
        return AccountSample(ts=int(now_ms),
                             equity=(float(facts.equity.value) if facts.equity.known else None),
                             balance=(float(facts.available_balance.value)
                                      if facts.available_balance.known else None),
                             position_qty=(float(facts.position_qty.value)
                                           if facts.position_qty.known else None),
                             exposure_total=(float(total) if total is not None else None),
                             exposure_confirmed=(float(confirmed) if confirmed is not None else None),
                             unrealized_pnl=(float(facts.unrealized_pnl.value)
                                             if facts.unrealized_pnl.known else None),
                             realized_pnl=(float(facts.realized_pnl.value)
                                           if facts.realized_pnl.known else None))

    # ------------------------------------------------------------------ 内部（显式、受守卫）

    def _scalar(self, target: object, name: str, anomalies: list[str]) -> Fact:
        """显式取值：属性或**显式调用**方法，只接受标量；异常 ⇒ UNKNOWN（不抛）。"""
        try:
            value = getattr(target, name)
        except Exception as exc:  # noqa: BLE001
            anomalies.append(f"{name}:{type(exc).__name__}")
            return Fact.unknown(f"{name} unavailable: {type(exc).__name__}")
        if callable(value):
            try:
                value = value()
            except Exception as exc:  # noqa: BLE001
                anomalies.append(f"{name}():{type(exc).__name__}")
                return Fact.unknown(f"{name}() raised {type(exc).__name__}")
        if value is None:
            return Fact.unknown(f"{name} is unknown (None)")
        if isinstance(value, _SCALARS):
            return Fact.of(value)
        # callable / 对象实例 / 内部引用一律不得进入产品事实
        anomalies.append(f"{name}:unsupported_type={type(value).__name__}")
        return Fact.unknown(f"{name} returned unsupported type {type(value).__name__}")

    def _position_facts(self, position: object | None) -> tuple[Fact, Fact]:
        """把 position 对象映射成 typed facts；**已知无仓位** 与 **未知** 必须区分。

        - qty 是标量（含 0.0）⇒ `position_known=known True`，`position_qty=known <qty>`；
        - 取不到 position / qty ⇒ 两者都是 UNKNOWN + reason（**绝不伪造 0**）。
        """
        if position is None:
            reason = "position is unavailable (no accounting position owner result)"
            return Fact.unknown(reason), Fact.unknown(reason)
        qty = getattr(position, "qty", None)
        if isinstance(qty, bool) or not isinstance(qty, _SCALARS):
            reason = ("position quantity is unknown" if qty is None
                      else f"position qty has unsupported type {type(qty).__name__}")
            return Fact.unknown(reason), Fact.unknown(reason)
        return Fact.of(True), Fact.of(float(qty))

    def _position(self, anomalies: list[str]) -> object | None:
        try:
            return self.accounting.position(self.symbol)              # 既有方法（显式调用）
        except Exception as exc:  # noqa: BLE001
            anomalies.append(f"position:{type(exc).__name__}")
            return None

    def _available_balance(self, anomalies: list[str], *, fallback: Fact) -> Fact:
        """可用余额优先取交易所事实（既有 `build_risk_snapshot` 契约），否则回落本地 balance。"""
        try:
            from risk.snapshot import build_risk_snapshot

            snapshot = build_risk_snapshot(self.accounting, symbol=self.symbol, now_ms=int(self.clock()))
            available = getattr(snapshot, "available_balance", None)
            if available is not None and isinstance(available, _SCALARS):
                return Fact.of(available)
        except Exception as exc:  # noqa: BLE001
            anomalies.append(f"risk_snapshot:{type(exc).__name__}")
        return fallback


__all__ = ["AccountingFacts", "AccountingFactsProvider"]
