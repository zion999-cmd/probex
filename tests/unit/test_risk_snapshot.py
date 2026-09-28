"""RiskSnapshot 构建：不可变输入、未知 ≠ 0、UTC 日界 helper（SC-10 的输入侧）。"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from market.events.types import Venue
from portfolio.accounting import AccountingCore
from portfolio.types import LiquidationInfo, Side
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import AvailableBalanceSource, ExchangeAvailableBalance
from tests.support import BASE_TS, make_fill, make_funding


def _core(*, balance: float = 10_000.0) -> AccountingCore:
    return AccountingCore(initial_balance=balance)


def _long_core(*, quantity: float = 1.0, price: float = 100.0, mark: float | None = 100.0, **kwargs):
    core = _core(**kwargs)
    core.record_fill(make_fill("f1", Side.BUY, price, quantity))
    if mark is not None:
        core.update_mark_price("BTCUSDT", mark, timestamp=BASE_TS + 1)
    return core


class SnapshotFieldsTest(unittest.TestCase):
    def test_snapshot_mirrors_accounting_facts(self) -> None:
        core = _long_core(mark=105.0)
        core.record_funding(make_funding(-2.0, timestamp=BASE_TS + 2))

        snapshot = build_risk_snapshot(
            core, symbol="BTCUSDT", now_ms=BASE_TS + 10, open_order_exposure=50.0, day_start_ts=BASE_TS
        )

        self.assertEqual(snapshot.symbol, "BTCUSDT")
        self.assertEqual(snapshot.now_ms, BASE_TS + 10)
        self.assertEqual(snapshot.balance, core.balance)
        self.assertEqual(snapshot.equity, core.equity())
        self.assertEqual(snapshot.position_qty, 1.0)
        self.assertEqual(snapshot.position_notional, 105.0)
        self.assertEqual(snapshot.gross_exposure, 105.0)
        self.assertEqual(snapshot.net_exposure, 105.0)
        self.assertEqual(snapshot.open_order_exposure, 50.0)
        self.assertEqual(snapshot.available_balance, core.balance - 50.0)
        self.assertEqual(snapshot.unrealized_pnl, 5.0)
        self.assertEqual(snapshot.mark_price, 105.0)
        self.assertEqual(snapshot.mark_age_ms, 9)
        self.assertEqual(snapshot.funding, -2.0)
        self.assertEqual(snapshot.trading_fees, 0.0)
        self.assertEqual(snapshot.net_realized, -2.0)
        self.assertEqual(snapshot.realized_pnl_today, -2.0)

    def test_snapshot_is_immutable(self) -> None:
        snapshot = build_risk_snapshot(_long_core(), symbol="BTCUSDT", now_ms=BASE_TS)
        with self.assertRaises(FrozenInstanceError):
            snapshot.equity = 0.0  # type: ignore[misc]

    def test_missing_mark_leaves_values_unknown(self) -> None:
        core = _long_core(mark=None)

        snapshot = build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS)

        self.assertIsNone(snapshot.mark_price)
        self.assertIsNone(snapshot.mark_age_ms)
        self.assertIsNone(snapshot.equity)
        self.assertIsNone(snapshot.unrealized_pnl)
        self.assertIsNone(snapshot.position_notional)
        self.assertIsNone(snapshot.drawdown)
        self.assertIsNone(snapshot.drawdown_pct)
        self.assertEqual(snapshot.balance, 10_000.0)  # balance 仍已知

    def test_daily_pnl_requires_injected_day_start(self) -> None:
        core = _long_core()
        core.record_fill(make_fill("f2", Side.SELL, 110.0, 1.0, exchange_ts=BASE_TS + 5_000))

        without = build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS + 6_000)
        with_start = build_risk_snapshot(
            core, symbol="BTCUSDT", now_ms=BASE_TS + 6_000, day_start_ts=BASE_TS + 1_000
        )

        self.assertIsNone(without.realized_pnl_today)
        self.assertEqual(with_start.realized_pnl_today, 10.0)

    def test_unknown_peak_equity_stays_unknown(self) -> None:
        """P0001.9.4 §2：历史峰值未知时**不得**用当前 equity 冒充峰值得出 drawdown = 0。"""
        core = _long_core(mark=105.0)
        # 尚无任何 equity 观测 ⇒ 峰值未知（不是 0）
        core_without_history = AccountingCore(initial_balance=100.0)

        snapshot = build_risk_snapshot(core_without_history, symbol="BTCUSDT", now_ms=BASE_TS)

        self.assertIsNone(snapshot.peak_equity)
        self.assertIsNone(snapshot.drawdown)
        self.assertIsNone(snapshot.drawdown_pct)

        # 已经观测过 equity 的正常会话：峰值照常来自观测
        dropped = build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS + 2)
        self.assertEqual(dropped.peak_equity, 10_005.0)
        self.assertEqual(dropped.drawdown, 0.0)

    def test_drawdown_after_equity_drop(self) -> None:
        core = _long_core(mark=200.0)
        core.update_mark_price("BTCUSDT", 150.0, timestamp=BASE_TS + 2)

        snapshot = build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS + 3)

        self.assertEqual(snapshot.peak_equity, 10_100.0)
        self.assertEqual(snapshot.drawdown, 50.0)
        assert snapshot.drawdown_pct is not None
        self.assertAlmostEqual(snapshot.drawdown_pct, 50.0 / 10_100.0)

    def test_liquidation_info_is_carried_through(self) -> None:
        liquidation = LiquidationInfo.from_prices(symbol="BTCUSDT", liquidation_price=90.0, mark_price=100.0)

        snapshot = build_risk_snapshot(
            _long_core(mark=100.0), symbol="BTCUSDT", now_ms=BASE_TS, liquidation=liquidation
        )

        assert snapshot.liquidation is not None
        self.assertEqual(snapshot.liquidation.distance_bps, 1_000.0)
        self.assertTrue(snapshot.liquidation.is_below_mark)

    def test_liquidation_symbol_mismatch_rejected(self) -> None:
        liquidation = LiquidationInfo.from_prices(symbol="ETHUSDT", liquidation_price=90.0, mark_price=100.0)
        with self.assertRaises(ValueError):
            build_risk_snapshot(
                _long_core(), symbol="BTCUSDT", now_ms=BASE_TS, liquidation=liquidation
            )

    def test_short_position_exposures(self) -> None:
        core = _core()
        core.record_fill(make_fill("f1", Side.SELL, 100.0, 2.0))
        core.update_mark_price("BTCUSDT", 90.0, timestamp=BASE_TS + 1)

        snapshot = build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS + 1)

        self.assertEqual(snapshot.position_qty, -2.0)
        self.assertEqual(snapshot.position_notional, 180.0)
        self.assertEqual(snapshot.gross_exposure, 180.0)
        self.assertEqual(snapshot.net_exposure, 180.0)
        self.assertEqual(snapshot.unrealized_pnl, 20.0)

    def test_validation(self) -> None:
        core = _long_core()
        with self.assertRaises(ValueError):
            build_risk_snapshot(core, symbol="", now_ms=BASE_TS)
        with self.assertRaises(ValueError):
            build_risk_snapshot(core, symbol="BTCUSDT", now_ms=-1)
        with self.assertRaises(ValueError):
            build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS, open_order_exposure=-1.0)
        with self.assertRaises(ValueError):
            build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS, day_start_ts=-5)


class UtcDayStartTest(unittest.TestCase):
    def test_floors_to_utc_midnight(self) -> None:
        midnight = 86_400_000 * 20_000
        self.assertEqual(utc_day_start_ms(midnight), midnight)
        self.assertEqual(utc_day_start_ms(midnight + 1), midnight)
        self.assertEqual(utc_day_start_ms(midnight + 86_399_999), midnight)
        self.assertEqual(utc_day_start_ms(midnight + 86_400_000), midnight + 86_400_000)

    def test_validation(self) -> None:
        for value in (-1, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    utc_day_start_ms(value)  # type: ignore[arg-type]

    def test_venue_and_symbol_are_not_touched(self) -> None:
        # 快照不引入 venue 维度（第一版 single symbol）
        snapshot = build_risk_snapshot(_long_core(), symbol="BTCUSDT", now_ms=BASE_TS)
        self.assertFalse(hasattr(snapshot, "venue"))
        self.assertIs(Venue.BINANCE.value, "binance")


class ExchangeAvailableBalanceTest(unittest.TestCase):
    """P0001.9.4 §3 / SC-4 / SC-5：live 口径必须来自交易所，Paper/Replay 语义不变。"""

    def test_local_derivation_is_the_default(self) -> None:
        core = _long_core(mark=100.0)

        snapshot = build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS, open_order_exposure=25.0)

        self.assertEqual(snapshot.available_balance, snapshot.balance - 25.0)
        self.assertIs(snapshot.available_balance_source, AvailableBalanceSource.LOCAL_DERIVED)
        self.assertFalse(snapshot.has_exchange_available_balance)
        self.assertIsNone(snapshot.available_balance_captured_at)
        self.assertIsNone(snapshot.available_balance_age_ms)

    def test_exchange_value_overrides_the_local_estimate(self) -> None:
        core = _long_core(mark=100.0)
        exchange = ExchangeAvailableBalance(value=777.0, captured_at=BASE_TS - 250)

        snapshot = build_risk_snapshot(
            core, symbol="BTCUSDT", now_ms=BASE_TS, open_order_exposure=25.0, exchange_available_balance=exchange
        )

        self.assertEqual(snapshot.available_balance, 777.0)  # 不是 balance - exposure
        self.assertIs(snapshot.available_balance_source, AvailableBalanceSource.BINANCE_ACCOUNT_SNAPSHOT)
        self.assertTrue(snapshot.has_exchange_available_balance)
        self.assertEqual(snapshot.available_balance_captured_at, BASE_TS - 250)
        self.assertEqual(snapshot.available_balance_age_ms, 250)

    def test_exchange_value_may_differ_from_local_derivation(self) -> None:
        """交易所可用余额可能因未实现盈亏/保证金占用而小于本地推导值——必须照实使用。"""
        core = _long_core(mark=100.0)
        local = build_risk_snapshot(core, symbol="BTCUSDT", now_ms=BASE_TS).available_balance

        exchange = ExchangeAvailableBalance(value=local - 100.0, captured_at=BASE_TS)
        snapshot = build_risk_snapshot(
            core, symbol="BTCUSDT", now_ms=BASE_TS, exchange_available_balance=exchange
        )

        self.assertLess(snapshot.available_balance, local)

    def test_contract_rejects_fake_sources_and_bad_values(self) -> None:
        with self.assertRaises(ValueError):
            ExchangeAvailableBalance(value=-1.0, captured_at=BASE_TS)
        with self.assertRaises(ValueError):
            ExchangeAvailableBalance(value=float("nan"), captured_at=BASE_TS)
        with self.assertRaises(ValueError):
            ExchangeAvailableBalance(value=1.0, captured_at=-1)
        with self.assertRaises(ValueError):
            ExchangeAvailableBalance(
                value=1.0, captured_at=BASE_TS, source=AvailableBalanceSource.LOCAL_DERIVED
            )

    def test_age_never_negative(self) -> None:
        exchange = ExchangeAvailableBalance(value=1.0, captured_at=BASE_TS + 10)

        self.assertEqual(exchange.age_ms(now_ms=BASE_TS), 0)


if __name__ == "__main__":
    unittest.main()
