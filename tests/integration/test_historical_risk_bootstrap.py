"""P0001.9.4.1 集成 / fault：历史 baseline 接线到 RiskSnapshot 与 readiness。

覆盖：

- SC-9：baseline cutoff 前历史 + cutoff 后本地账本**不重复计账**；
- SC-10：bootstrap 重跑幂等；
- SC-11：完整零流水窗口 = 已知的 0（不是 UNKNOWN）；
- SC-12：history 不完整 ⇒ 仍为 UNKNOWN（不得用部分值）；
- SC-13：`RiskSnapshot.realized_pnl_today` 使用可信 historical baseline ⇒ readiness 的
  `HISTORICAL_DAILY_PNL_UNKNOWN` 被真实解除；
- SC-14：Paper / Replay 路径不变；
- SC-15：`peak_equity` / `drawdown` 仍为 UNKNOWN（绝不伪造）。
"""

from __future__ import annotations

import unittest

from connectors.binance.private.income import (
    IncomeHistoryCoverage,
    IncomeHistoryFacts,
    UnsupportedIncomeAssetError,
    fetch_income_history,
    parse_income_rows,
    verify_settlement_asset,
)
from connectors.binance.private.rest import PrivateRestClient
from market.events.types import Milliseconds
from portfolio.accounting import AccountingCore
from portfolio.types import ExternalAccountBaseline, Side
from readiness import (
    HistoricalRiskEvidence,
    LiveReadinessEvidence,
    LiveReadinessGate,
    LiveReadinessReason,
    ReadinessPolicy,
    historical_risk_baseline_from_income,
    historical_risk_evidence_from_snapshot,
)
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import AvailableBalanceSource
from tests.readiness_support import active_hwm
from tests.private_support import FakeRestFetcher, credentials
from tests.support import BASE_TS, SYMBOL, make_fill

DAY = 86_400_000
#: 测试用阈值（非业务决策值）。
POLICY = ReadinessPolicy(
    max_clock_uncertainty_ms=1_000,
    max_median_private_lag_ms=5_000,
    max_calibration_age_ms=3_600_000,
    max_available_balance_age_ms=60_000,
)


def income_row(
    *, income_type: str = "REALIZED_PNL", income: str = "10.0", time_ms: int = BASE_TS - 100, tran_id: int = 1
) -> dict:
    return {
        "symbol": "" if income_type == "TRANSFER" else SYMBOL,
        "incomeType": income_type,
        "income": income,
        "asset": "USDT",
        "time": time_ms,
        "info": income_type,
        "tradeId": "" if income_type == "TRANSFER" else f"t{tran_id}",
        "tranId": tran_id,
    }


def build_facts(rows: list[dict], *, complete: bool = True, reason: str = "") -> IncomeHistoryFacts:
    parsed = parse_income_rows(rows)
    times = [row.time_ms for row in parsed]
    return IncomeHistoryFacts(
        rows=parsed,
        coverage=IncomeHistoryCoverage(
            start_ts=min(times) if times else BASE_TS - DAY,
            end_ts=max(times) if times else BASE_TS,
            rows=len(parsed),
            pages=1,
            complete=complete,
            reason=reason,
        ),
        duplicates_dropped=0,
    )


class BaselineCompositionTest(unittest.TestCase):
    """SC-9 / SC-13：外部历史与本地账本的水位线拼接。"""

    def _bootstrapped_accounting(self) -> AccountingCore:
        core = AccountingCore(initial_balance=0.0)
        core.bootstrap_from_baseline(
            ExternalAccountBaseline(
                symbol=SYMBOL,
                wallet_balance=5_000.0,
                available_balance=5_000.0,
                position_qty=0.0,
                entry_price=0.0,
                mark_price=0.0,
                liquidation_price=0.0,
                captured_at=BASE_TS,
            )
        )
        return core

    def test_sc9_history_and_local_are_composed_without_double_counting(self) -> None:
        accounting = self._bootstrapped_accounting()
        # cutoff 之后的本地成交（应计入本地段）
        accounting.record_fill(make_fill("f-after", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS + 10))
        cutoff = BASE_TS + 5
        facts = build_facts([income_row(income="10.0", tran_id=1), income_row(income_type="COMMISSION", income="-0.5", tran_id=2)])
        baseline = historical_risk_baseline_from_income(facts, day_start_ts=BASE_TS - DAY, cutoff_ts=cutoff)

        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=cutoff + 100, historical_baseline=baseline
        )

        self.assertAlmostEqual(baseline.daily_net_realized or 0.0, 9.5)
        self.assertTrue(baseline.daily_pnl_known)
        self.assertAlmostEqual(snapshot.realized_pnl_today or 0.0, 9.5)  # 本地段该成交尚未实现盈亏（开仓）
        self.assertEqual(historical_risk_evidence_from_snapshot(snapshot).daily_pnl_known, True)

    def test_sc9_local_closed_trade_adds_to_history(self) -> None:
        accounting = self._bootstrapped_accounting()
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS + 1))
        accounting.record_fill(make_fill("f2", Side.SELL, 110.0, 1.0, exchange_ts=BASE_TS + 2))
        cutoff = BASE_TS
        facts = build_facts([income_row(income="10.0", tran_id=1)])
        baseline = historical_risk_baseline_from_income(facts, day_start_ts=BASE_TS - DAY, cutoff_ts=cutoff)

        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=cutoff + 100, historical_baseline=baseline
        )

        # 历史段 10 + 本地已实现 10（平仓收益）——两段互不重叠
        self.assertAlmostEqual(snapshot.realized_pnl_today or 0.0, 20.0)

    def test_before_cutoff_local_fills_are_not_double_counted(self) -> None:
        """cutoff 之前的成交不由本地账本重复贡献（它们已在 baseline 里）。"""
        accounting = self._bootstrapped_accounting()
        cutoff = BASE_TS + 1_000
        facts = build_facts([income_row(income="3.0", tran_id=1)])
        baseline = historical_risk_baseline_from_income(facts, day_start_ts=BASE_TS - DAY, cutoff_ts=cutoff)

        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=cutoff + 10, historical_baseline=baseline
        )

        self.assertAlmostEqual(snapshot.realized_pnl_today or 0.0, 3.0)

    def test_cutoff_before_baseline_watermark_keeps_pnl_unknown(self) -> None:
        """cutoff 早于 accounting baseline 水位线 ⇒ 本地段未知 ⇒ 当日 PnL 未知（fail closed）。"""
        accounting = self._bootstrapped_accounting()  # baseline captured_at = BASE_TS
        facts = build_facts([income_row(income="3.0", tran_id=1)])
        baseline = historical_risk_baseline_from_income(facts, day_start_ts=BASE_TS - DAY, cutoff_ts=BASE_TS - 10)

        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS, historical_baseline=baseline
        )

        self.assertIsNone(snapshot.realized_pnl_today)
        self.assertEqual(historical_risk_evidence_from_snapshot(snapshot).daily_pnl_known, False)


class FailClosedTest(unittest.TestCase):
    """SC-7 / SC-8 / SC-11 / SC-12 / SC-15。"""

    def _snapshot(self, facts: IncomeHistoryFacts, *, cutoff: int = BASE_TS):
        accounting = AccountingCore(initial_balance=1_000.0)
        baseline = historical_risk_baseline_from_income(facts, day_start_ts=BASE_TS - DAY, cutoff_ts=cutoff)
        return build_risk_snapshot(accounting, symbol=SYMBOL, now_ms=cutoff, historical_baseline=baseline), baseline

    def test_sc11_complete_empty_window_is_known_zero(self) -> None:
        snapshot, baseline = self._snapshot(build_facts([]))

        self.assertTrue(baseline.daily_pnl_known)
        self.assertEqual(snapshot.realized_pnl_today, 0.0)
        self.assertEqual(historical_risk_evidence_from_snapshot(snapshot).daily_pnl_known, True)

    def test_sc12_incomplete_history_stays_unknown(self) -> None:
        facts = build_facts([income_row(income="5.0")], complete=False, reason="exceeded max_pages=2")
        snapshot, baseline = self._snapshot(facts)

        self.assertFalse(baseline.daily_pnl_known)
        self.assertIsNone(snapshot.realized_pnl_today)  # 不用部分值
        self.assertIn("incomplete", baseline.detail)
        self.assertEqual(historical_risk_evidence_from_snapshot(snapshot).daily_pnl_known, False)

    def test_sc7_unclassified_income_blocks_the_baseline(self) -> None:
        facts = build_facts([income_row(income_type="INSURANCE_CLEAR", income="-1.0", tran_id=9)])
        _snapshot_, baseline = self._snapshot(facts)

        self.assertFalse(baseline.daily_pnl_known)
        self.assertIn("UNCLASSIFIED_INCOME", baseline.detail)

    def test_sc8_non_usdt_trading_income_raises(self) -> None:
        rows = parse_income_rows([{**income_row(), "asset": "BTC"}])

        with self.assertRaises(UnsupportedIncomeAssetError):
            verify_settlement_asset(rows)

    def test_sc6_transfer_does_not_enter_trading_pnl(self) -> None:
        facts = build_facts(
            [
                income_row(income_type="TRANSFER", income="5000.0", tran_id=0),
                income_row(income_type="REALIZED_PNL", income="1.25", tran_id=1),
            ]
        )
        snapshot, baseline = self._snapshot(facts)

        self.assertAlmostEqual(baseline.daily_net_realized or 0.0, 1.25)
        self.assertEqual(baseline.non_trading_rows, 1)
        self.assertAlmostEqual(snapshot.realized_pnl_today or 0.0, 1.25)

    def test_sc15_peak_and_drawdown_remain_unknown(self) -> None:
        facts = build_facts([income_row(income="100.0")])
        snapshot, baseline = self._snapshot(facts)

        self.assertIsNone(snapshot.peak_equity)
        self.assertIsNone(snapshot.drawdown)
        self.assertIsNone(snapshot.drawdown_pct)
        self.assertFalse(baseline.drawdown_known)
        self.assertFalse(baseline.peak_equity_known)
        evidence = historical_risk_evidence_from_snapshot(snapshot)
        self.assertTrue(evidence.daily_pnl_known)
        self.assertFalse(evidence.drawdown_known)


class ReadinessIntegrationTest(unittest.TestCase):
    """SC-13：可信 baseline 让 `HISTORICAL_DAILY_PNL_UNKNOWN` 真实解除（其余阻塞项仍在）。"""

    def _evidence(self, snapshot, baseline) -> LiveReadinessEvidence:
        from readiness import (
            AccountEvidence,
            Environment,
            EnvironmentEvidence,
            PrivateStreamEvidence,
        )
        from connectors.binance.private.auth import ClockCalibration
        from connectors.binance.private.recovery import RecoveryStatus
        from risk.types import KillSwitchMode
        from readiness import LiveRiskPolicy

        risk_policy = LiveRiskPolicy(
            max_position_qty=1.0,
            max_order_notional=1_000.0,
            max_open_order_exposure=1_000.0,
            max_daily_loss=100.0,
            max_drawdown_pct=0.2,
            max_leverage=3.0,
            max_mark_age_ms=5_000,
            kill_switch_mode=KillSwitchMode.NORMAL,
        )
        return LiveReadinessEvidence(
            now_ms=BASE_TS,
            recovery_status=RecoveryStatus.RECOVERED,
            private_stream=PrivateStreamEvidence(
                listen_key_state="ACTIVE",
                continuity_assumed=True,
                boundary_present=True,
                median_private_lag_ms=50,
                clock_calibration=ClockCalibration(
                    offset_ms=0, round_trip_ms=20, uncertainty_ms=10, measured_at_ms=BASE_TS
                ),
            ),
            account=AccountEvidence(can_trade=True, available_balance=1_000.0, available_balance_captured_at=BASE_TS),
            historical_risk=historical_risk_evidence_from_snapshot(snapshot),
            environment=EnvironmentEvidence(environment=Environment.TESTNET),
            market_ready=True,
            risk_policy=risk_policy,
            high_watermark=active_hwm(),
        )

    def test_daily_pnl_unknown_is_lifted_but_drawdown_still_blocks(self) -> None:
        accounting = AccountingCore(initial_balance=1_000.0)
        facts = build_facts([income_row(income="12.5")])
        baseline = historical_risk_baseline_from_income(facts, day_start_ts=BASE_TS - DAY, cutoff_ts=BASE_TS)
        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS, historical_baseline=baseline
        )

        result = LiveReadinessGate(policy=POLICY).evaluate(self._evidence(snapshot, baseline))

        self.assertNotIn(LiveReadinessReason.HISTORICAL_DAILY_PNL_UNKNOWN, result.reasons)
        self.assertIn(LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN, result.reasons)

    def test_incomplete_history_keeps_daily_pnl_unknown(self) -> None:
        accounting = AccountingCore(initial_balance=1_000.0)
        facts = build_facts([income_row(income="12.5")], complete=False, reason="read failed")
        baseline = historical_risk_baseline_from_income(facts, day_start_ts=BASE_TS - DAY, cutoff_ts=BASE_TS)
        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS, historical_baseline=baseline
        )

        result = LiveReadinessGate(policy=POLICY).evaluate(self._evidence(snapshot, baseline))

        self.assertIn(LiveReadinessReason.HISTORICAL_DAILY_PNL_UNKNOWN, result.reasons)


class PaperPathUnchangedTest(unittest.TestCase):
    """SC-14：不传 baseline 时行为与 P0001.5 完全一致（Paper / Replay 确定性不变）。"""

    def test_local_path_is_unchanged(self) -> None:
        accounting = AccountingCore(initial_balance=1_000.0)
        accounting.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS)
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        accounting.record_fill(make_fill("f2", Side.SELL, 101.0, 1.0, exchange_ts=BASE_TS + 1))
        day_start = utc_day_start_ms(BASE_TS)

        without = build_risk_snapshot(accounting, symbol=SYMBOL, now_ms=BASE_TS + 10, day_start_ts=day_start)
        explicit_none = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS + 10, day_start_ts=day_start, historical_baseline=None
        )

        self.assertIs(without.available_balance_source, AvailableBalanceSource.LOCAL_DERIVED)
        self.assertEqual(without.realized_pnl_today, explicit_none.realized_pnl_today)
        self.assertEqual(without.realized_pnl_today, accounting.net_realized_since(day_start))


class PaginationIntegrationTest(unittest.TestCase):
    """SC-2 的真实 REST 形状（多页 > 单页上限）走完整分页。"""

    def test_multi_page_over_limit(self) -> None:
        def batch(start: int, count: int) -> list[dict]:
            return [
                income_row(tran_id=start + index, time_ms=BASE_TS + start + index) for index in range(count)
            ]

        fetcher = FakeRestFetcher()
        client = PrivateRestClient(credentials=credentials(), fetcher=fetcher)
        fetcher.sequences["/fapi/v1/income"] = [batch(0, 1_000), batch(1_000, 500)]

        facts = fetch_income_history(
            client, window_start_ms=BASE_TS, cutoff_ms=BASE_TS + 100_000, page_limit=1_000, max_pages=3
        )

        self.assertTrue(facts.coverage.complete)
        self.assertEqual(facts.coverage.pages, 2)
        self.assertEqual(facts.coverage.rows, 1_500)
        self.assertEqual(fetcher.calls.count(("GET", "/fapi/v1/income")), 2)


if __name__ == "__main__":
    unittest.main()
