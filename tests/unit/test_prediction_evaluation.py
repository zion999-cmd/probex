"""预测复盘评估单元测试（D-055 批次3）。

方向度量无需 bucket 数值边界（严格符号）；五分类 Brier 只在显式给边界时计算；
adverse-selection 复用既有阈值配置。
"""

from __future__ import annotations

import unittest

from prediction.evaluation import PredictionEvaluator
from prediction.types import FutureReturnDistribution, Prediction
from tests.strategy_support import make_record

T0 = 1_700_000_000_000


def _dist(horizon_ms: int, *, strong_down=0.05, down=0.15, flat=0.1,
            up=0.5, strong_up=0.2) -> FutureReturnDistribution:
    return FutureReturnDistribution(
        horizon_ms=horizon_ms, strong_down=strong_down, down=down, flat=flat,
        up=up, strong_up=strong_up)


def _record(*distributions, sell_as=0.9, buy_as=0.1) -> object:
    prediction = Prediction(
        future_return=tuple(distributions),
        buy_adverse_selection=buy_as, sell_adverse_selection=sell_as,
        buy_fill_probability=0.5, sell_fill_probability=0.5,
        provider_confidence=0.8, derived_confidence=0.7)
    return make_record(prediction=prediction, now_ms=T0)


class PredictionEvaluatorDirectionTest(unittest.TestCase):
    def test_up_move_scores_direction_hit_and_brier(self) -> None:
        evaluator = PredictionEvaluator()
        record = _record(_dist(5_000))
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(record, mid=100.0)
        evaluator.observe_mid(T0 + 5_000, 100.1)  # +10 bps
        result = evaluator.evaluate()
        self.assertEqual(result.predictions_seen, 1)
        outcome = result.horizons[0]
        self.assertEqual(outcome.samples, 1)
        self.assertEqual(outcome.horizon_ms, 5_000)
        self.assertEqual(outcome.direction_hit_rate, 1.0)
        # p_up=0.7, p_flat=0.1, p_down=0.2；realized up
        expected = 0.3 ** 2 + 0.1 ** 2 + 0.2 ** 2
        self.assertAlmostEqual(outcome.brier_sign_mean, expected, places=9)

    def test_down_move_with_down_probability_dominant_hits(self) -> None:
        evaluator = PredictionEvaluator()
        record = _record(_dist(5_000, strong_down=0.4, down=0.4, flat=0.1,
                                  up=0.05, strong_up=0.05))
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(record, mid=100.0)
        evaluator.observe_mid(T0 + 5_000, 99.9)
        result = evaluator.evaluate()
        self.assertEqual(result.horizons[0].direction_hit_rate, 1.0)

    def test_flat_prediction_vs_strict_zero_move(self) -> None:
        evaluator = PredictionEvaluator()
        record = _record(_dist(5_000, strong_down=0.05, down=0.05, flat=0.8,
                                  up=0.05, strong_up=0.05))
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(record, mid=100.0)
        evaluator.observe_mid(T0 + 5_000, 100.0)  # 严格 0 才是 flat
        self.assertEqual(evaluator.evaluate().horizons[0].direction_hit_rate, 1.0)

    def test_prediction_without_future_observation_is_excluded(self) -> None:
        evaluator = PredictionEvaluator()
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(_record(_dist(60_000)), mid=100.0)
        evaluator.observe_mid(T0 + 30_000, 100.1)
        result = evaluator.evaluate()
        self.assertEqual(result.predictions_seen, 1)
        self.assertEqual(result.horizons, ())  # 唯一 horizon 60s 不可评估

    def test_uses_first_mid_at_or_after_horizon(self) -> None:
        evaluator = PredictionEvaluator()
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(_record(_dist(5_000)), mid=100.0)
        evaluator.observe_mid(T0 + 4_999, 101.0)  # 窗口内忽略
        evaluator.observe_mid(T0 + 5_001, 100.1)  # +10 bps 计入
        outcome = evaluator.evaluate().horizons[0]
        self.assertEqual(outcome.samples, 1)
        self.assertEqual(outcome.direction_hit_rate, 1.0)

    def test_non_monotonic_mid_observations_rejected(self) -> None:
        evaluator = PredictionEvaluator()
        evaluator.observe_mid(T0 + 1_000, 100.0)
        with self.assertRaises(ValueError):
            evaluator.observe_mid(T0, 99.0)


class AdverseSelectionEvaluationTest(unittest.TestCase):
    def test_sell_noul_binary_scoring(self) -> None:
        evaluator = PredictionEvaluator(adverse_selection_threshold_bps=5.0)
        record = _record(_dist(5_000), sell_as=0.9, buy_as=0.1)
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(record, mid=100.0)
        evaluator.observe_mid(T0 + 5_000, 99.9)  # -10 bps ⇒ sell AS 事件发生
        result = evaluator.evaluate()
        by_name = {b.name: b for b in result.adverse_selection}
        self.assertAlmostEqual(by_name["sell_adverse_selection"].brier_mean, 0.01, places=9)
        self.assertEqual(by_name["sell_adverse_selection"].hit_rate, 1.0)
        self.assertEqual(by_name["buy_adverse_selection"].samples, 1)
        self.assertEqual(by_name["sell_adverse_selection"].threshold_bps, 5.0)

    def test_no_threshold_means_no_as_scoring(self) -> None:
        evaluator = PredictionEvaluator()
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(_record(_dist(5_000)), mid=100.0)
        evaluator.observe_mid(T0 + 5_000, 99.0)
        result = evaluator.evaluate()
        self.assertEqual(result.adverse_selection, ())
        self.assertTrue(any("adverse-selection" in n for n in result.notes))


class FiveClassEvaluationTest(unittest.TestCase):
    def test_five_class_scored_only_with_configured_boundaries(self) -> None:
        evaluator = PredictionEvaluator(bucket_thresholds_bps=(10.0, 30.0))
        record = _record(_dist(5_000, strong_down=0.05, down=0.1, flat=0.1,
                                  up=0.55, strong_up=0.2))
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(record, mid=100.0)
        evaluator.observe_mid(T0 + 5_000, 100.2)  # +20 bps ⇒ up（index 3）
        result = evaluator.evaluate()
        self.assertEqual(len(result.five_class), 1)
        self.assertEqual(result.five_class[0].argmax_hit_rate, 1.0)

    def test_unconfigured_boundaries_emits_note_only(self) -> None:
        evaluator = PredictionEvaluator()
        evaluator.observe_mid(T0, 100.0)
        evaluator.add_prediction(_record(_dist(5_000)), mid=100.0)
        evaluator.observe_mid(T0 + 5_000, 100.2)
        result = evaluator.evaluate()
        self.assertEqual(result.five_class, ())
        self.assertTrue(any("five-class" in n for n in result.notes))


if __name__ == "__main__":
    unittest.main()
