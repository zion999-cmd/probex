"""SC-7 / SC-8：严格 parser 与置信度来源。"""

from __future__ import annotations

import json
import math
import unittest

from prediction.errors import PredictionInvalidResponseError, PredictionParseError
from prediction.parsing.market_v1 import parse_jev_prediction
from prediction.schema.market_v1 import QUESTION_SCHEMA_VERSION
from prediction.types import FUTURE_RETURN_CATEGORIES
from tests.fakes import response_json, response_payload


def _payload() -> dict:
    return json.loads(json.dumps(response_payload()))


def _text(payload: object) -> str:
    return json.dumps(payload, separators=(",", ":"))


class StrictParserTest(unittest.TestCase):
    def test_valid_response_parses(self) -> None:
        prediction = parse_jev_prediction(response_json())

        self.assertEqual(len(prediction.future_return), 4)
        self.assertEqual([d.horizon_ms for d in prediction.future_return], [5_000, 15_000, 30_000, 60_000])
        self.assertEqual(prediction.buy_adverse_selection, 0.5)
        self.assertEqual(prediction.sell_fill_probability, 0.5)
        for distribution in prediction.future_return:
            for category in FUTURE_RETURN_CATEGORIES:
                self.assertGreaterEqual(getattr(distribution, category), 0.0)

    def test_malformed_json_is_parse_error(self) -> None:
        for raw in ("", "   ", "{not json", "null", "[1,2,3]", '"text"', "42"):
            with self.subTest(raw=raw):
                with self.assertRaises((PredictionParseError, PredictionInvalidResponseError)):
                    parse_jev_prediction(raw)

    def test_wrong_schema_version_is_invalid(self) -> None:
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(response_json(question_schema_version="jev-market-v2"))
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(response_json(question_schema_version=""))
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(json.dumps({"future_return": {}}))

    def test_expected_version_can_be_overridden(self) -> None:
        prediction = parse_jev_prediction(
            response_json(question_schema_version="jev-market-v9"),
            expected_question_schema_version="jev-market-v9",
        )
        self.assertEqual(len(prediction.future_return), 4)

    def test_missing_horizon_is_invalid(self) -> None:
        payload = _payload()
        del payload["future_return"]["60s"]
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_unknown_horizon_is_invalid(self) -> None:
        payload = _payload()
        payload["future_return"]["120s"] = dict(payload["future_return"]["5s"])
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_missing_category_is_invalid(self) -> None:
        payload = _payload()
        del payload["future_return"]["5s"]["flat"]
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_unknown_category_is_invalid(self) -> None:
        payload = _payload()
        payload["future_return"]["5s"]["sideways"] = 0.0
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_missing_probability_question_is_invalid(self) -> None:
        payload = _payload()
        del payload["buy_fill_probability"]
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_unknown_top_level_field_is_invalid(self) -> None:
        payload = _payload()
        payload["advice"] = "buy"
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_duplicate_key_is_invalid(self) -> None:
        raw = (
            '{"question_schema_version":"jev-market-v1",'
            '"buy_fill_probability":0.5,"buy_fill_probability":0.6}'
        )
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(raw)

    def test_non_finite_numbers_are_invalid(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                payload = _payload()
                payload["buy_fill_probability"] = constant
                with self.assertRaises(PredictionInvalidResponseError):
                    parse_jev_prediction(_text(payload).replace(f'"{constant}"', constant))

    def test_out_of_range_probabilities_are_invalid(self) -> None:
        for value in (-0.1, 1.1, 2, -1):
            with self.subTest(value=value):
                payload = _payload()
                payload["buy_adverse_selection"] = value
                with self.assertRaises(PredictionInvalidResponseError):
                    parse_jev_prediction(_text(payload))

    def test_non_numeric_probability_is_invalid(self) -> None:
        for value in ("0.5", None, True, [], {}):
            with self.subTest(value=value):
                payload = _payload()
                payload["buy_adverse_selection"] = value
                with self.assertRaises(PredictionInvalidResponseError):
                    parse_jev_prediction(_text(payload))

    def test_probability_sum_outside_tolerance_is_invalid(self) -> None:
        payload = _payload()
        payload["future_return"]["5s"]["flat"] = 0.9
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_probability_sum_inside_tolerance_is_accepted(self) -> None:
        payload = _payload()
        payload["future_return"]["5s"]["flat"] = 0.6 + 1e-9
        prediction = parse_jev_prediction(_text(payload))
        self.assertEqual(len(prediction.future_return), 4)

    def test_confidence_out_of_range_is_invalid(self) -> None:
        payload = _payload()
        payload["confidence"] = 1.5
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_future_return_must_be_object(self) -> None:
        payload = _payload()
        payload["future_return"] = 0.5
        with self.assertRaises(PredictionInvalidResponseError):
            parse_jev_prediction(_text(payload))

    def test_parser_never_invents_missing_values(self) -> None:
        payload = _payload()
        del payload["sell_adverse_selection"]
        with self.assertRaises(PredictionInvalidResponseError) as context:
            parse_jev_prediction(_text(payload))
        self.assertIn("sell_adverse_selection", str(context.exception))


class ConfidenceSourceTest(unittest.TestCase):
    """SC-8 验收：provider_confidence 与 derived_confidence 来源可区分。"""

    def test_provider_confidence_is_kept_when_present(self) -> None:
        prediction = parse_jev_prediction(response_json(confidence=0.7))
        self.assertEqual(prediction.provider_confidence, 0.7)
        self.assertIsNotNone(prediction.derived_confidence)

    def test_provider_confidence_absent_is_none_but_derived_is_computed(self) -> None:
        prediction = parse_jev_prediction(response_json())
        self.assertIsNone(prediction.provider_confidence)
        self.assertIsNotNone(prediction.derived_confidence)

    def test_derived_confidence_is_zero_for_uniform_distribution(self) -> None:
        prediction = parse_jev_prediction(response_json(flat=0.2))
        self.assertAlmostEqual(prediction.derived_confidence, 0.0)

    def test_derived_confidence_is_one_for_certain_distribution(self) -> None:
        prediction = parse_jev_prediction(response_json(flat=1.0))
        self.assertAlmostEqual(prediction.derived_confidence, 1.0)

    def test_derived_confidence_matches_entropy_formula(self) -> None:
        prediction = parse_jev_prediction(response_json(flat=0.6))
        distribution = prediction.distribution(5_000)
        assert distribution is not None
        expected = 1.0 - distribution.entropy / math.log(len(FUTURE_RETURN_CATEGORIES))
        self.assertAlmostEqual(prediction.derived_confidence, expected)

    def test_two_sources_are_independent(self) -> None:
        prediction = parse_jev_prediction(response_json(flat=1.0, confidence=0.1))
        self.assertEqual(prediction.provider_confidence, 0.1)
        self.assertAlmostEqual(prediction.derived_confidence, 1.0)
        self.assertNotEqual(prediction.provider_confidence, prediction.derived_confidence)


if __name__ == "__main__":
    unittest.main()
