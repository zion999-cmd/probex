"""P0001.11 §3 / 裁决 A：Metric Contract（公式 / 时间基准 / Owner / UNKNOWN 条件 / 采样）。"""

from __future__ import annotations

import unittest

from product.types import Fact
from reports.metrics import (
    METRIC_DEFINITIONS,
    METRIC_NAMES,
    SHARPE_MIN_SAMPLES,
    EquitySample,
    RealizedTradeResult,
    compute_metrics,
    definitions_payload,
    profit_factor,
    resample_equity,
    run_mdd,
    sharpe,
)


def series(count: int, *, step: float = 1.0, start_ts: int = 0, interval_ms: int = 60_000):
    return [EquitySample(ts=start_ts + index * interval_ms, equity=1_000.0 + index * step)
            for index in range(count)]


class MetricDefinitionTest(unittest.TestCase):
    def test_every_metric_documents_formula_timebase_owner_unknown_and_sampling(self) -> None:
        for definition in METRIC_DEFINITIONS:
            with self.subTest(metric=definition.name):
                for field in ("formula", "time_base", "fact_owner", "unknown_condition", "sampling"):
                    self.assertTrue(getattr(definition, field).strip())
        self.assertEqual(set(METRIC_NAMES), {d.name for d in METRIC_DEFINITIONS})

    def test_required_metric_names_are_present(self) -> None:
        for name in ("net_pnl", "realized_pnl", "unrealized_pnl", "fees", "funding",
                     "max_confirmed_exposure", "max_total_exposure", "run_mdd", "sharpe", "profit_factor"):
            self.assertIn(name, METRIC_NAMES)

    def test_run_mdd_is_named_and_documented_as_run_internal(self) -> None:
        definition = next(d for d in METRIC_DEFINITIONS if d.name == "run_mdd")
        self.assertIn("run", definition.label.lower())
        self.assertIn("trusted activation point", definition.time_base)
        # 命名/文档必须明确"不是风控权威"（中文或英文任一表述均可）
        self.assertTrue("非" in definition.time_base or "not" in definition.time_base.lower())

    def test_definitions_payload_is_serializable(self) -> None:
        payload = definitions_payload()
        self.assertEqual(len(payload), len(METRIC_DEFINITIONS))
        self.assertTrue(all(isinstance(item, dict) for item in payload))


class SharpeTest(unittest.TestCase):
    def test_sharpe_needs_at_least_thirty_samples(self) -> None:
        self.assertFalse(sharpe(series(SHARPE_MIN_SAMPLES - 1)).known)
        self.assertIn("fewer than", sharpe(series(SHARPE_MIN_SAMPLES - 1)).reason)
        self.assertTrue(sharpe(series(SHARPE_MIN_SAMPLES + 1)).known)

    def test_flat_equity_has_undefined_sharpe(self) -> None:
        value = sharpe([EquitySample(ts=index * 60_000, equity=1_000.0) for index in range(40)])
        self.assertFalse(value.known)
        self.assertIn("stdev", value.reason)

    def test_sharpe_is_not_annualized(self) -> None:
        definition = next(d for d in METRIC_DEFINITIONS if d.name == "sharpe")
        self.assertIn("未做年化", definition.formula)
        self.assertIn("1 分钟", definition.sampling)

    def test_single_timestamp_is_unknown(self) -> None:
        value = sharpe([EquitySample(ts=0, equity=1.0)])
        self.assertFalse(value.known)

    def test_resampling_is_step_function_without_interpolation(self) -> None:
        series_out, reason = resample_equity([EquitySample(ts=0, equity=100.0),
                                              EquitySample(ts=120_000, equity=200.0)], interval_ms=60_000)
        self.assertIsNone(reason)
        self.assertEqual([(s.ts, s.equity) for s in series_out], [(0, 100.0), (60_000, 100.0), (120_000, 200.0)])


class RunMddTest(unittest.TestCase):
    def test_run_mdd_is_peak_to_trough_within_the_run(self) -> None:
        value = run_mdd([EquitySample(ts=0, equity=100.0), EquitySample(ts=60_000, equity=120.0),
                         EquitySample(ts=120_000, equity=90.0)])
        self.assertTrue(value.known)
        self.assertAlmostEqual(value.value, 0.25)

    def test_run_mdd_requires_two_samples(self) -> None:
        self.assertFalse(run_mdd([EquitySample(ts=0, equity=100.0)]).known)

    def test_non_positive_peak_is_unknown(self) -> None:
        self.assertFalse(run_mdd([EquitySample(ts=0, equity=0.0), EquitySample(ts=1, equity=0.0)]).known)


class ProfitFactorTest(unittest.TestCase):
    def test_profit_factor_uses_realized_results_only(self) -> None:
        value = profit_factor([RealizedTradeResult(2.0), RealizedTradeResult(-1.0),
                               RealizedTradeResult(1.0)])
        self.assertTrue(value.known)
        self.assertAlmostEqual(value.value, 3.0)

    def test_no_losing_trades_is_unknown_not_infinity(self) -> None:
        value = profit_factor([RealizedTradeResult(1.0)])
        self.assertFalse(value.known)
        self.assertIn("no losing trades", value.reason)

    def test_no_trades_is_unknown(self) -> None:
        self.assertFalse(profit_factor([]).known)


class ComputeMetricsTest(unittest.TestCase):
    def test_authoritative_accounting_facts_are_passed_through(self) -> None:
        metrics = compute_metrics(owner_facts={"net_pnl": 1.5, "realized_pnl": 2.0, "unrealized_pnl": -0.5,
                                               "fees": 0.02, "funding": 0.0},
                                  max_confirmed_exposure=80.0, max_total_exposure=95.0)
        self.assertEqual(metrics.fact("net_pnl").value, 1.5)
        self.assertEqual(metrics.fact("fees").value, 0.02)
        self.assertEqual(metrics.fact("max_confirmed_exposure").value, 80.0)
        self.assertEqual(metrics.fact("max_total_exposure").value, 95.0)

    def test_missing_accounting_facts_stay_unknown_and_are_never_derived(self) -> None:
        metrics = compute_metrics()
        for name in ("net_pnl", "realized_pnl", "unrealized_pnl", "fees", "funding"):
            with self.subTest(metric=name):
                fact = metrics.fact(name)
                self.assertFalse(fact.known)
                self.assertIsNone(fact.value)
                self.assertIn("never derive", fact.reason)

    def test_both_exposure_metrics_are_kept_separately(self) -> None:
        metrics = compute_metrics(max_confirmed_exposure=10.0, max_total_exposure=25.0)
        self.assertNotEqual(metrics.fact("max_confirmed_exposure").value,
                            metrics.fact("max_total_exposure").value)
        self.assertFalse(metrics.fact("max_total_exposure").known is False)

    def test_unknown_metric_name_is_unknown_not_zero(self) -> None:
        self.assertEqual(compute_metrics().fact("nope"), Fact.unknown("unknown metric 'nope'"))

    def test_known_names_lists_only_known_metrics(self) -> None:
        metrics = compute_metrics(owner_facts={"fees": 0.0})
        self.assertIn("fees", metrics.known_names())
        self.assertNotIn("net_pnl", metrics.known_names())
