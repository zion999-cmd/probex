"""question schema：版本、问题集合、canonical 规则与请求构造。"""

from __future__ import annotations

import json
import unittest

from storage.events.codec import canonical_json as storage_canonical_json
from prediction.schema.market_v1 import (
    FUTURE_RETURN_HORIZONS_MS,
    PROBABILITY_QUESTIONS,
    QUESTION_SCHEMA_VERSION,
    QUESTION_SPECS,
    QUESTION_SPEC_BY_KEY,
    build_prediction_request,
    canonical_json,
    horizon_from_key,
    horizon_key,
)
from prediction.types import FUTURE_RETURN_CATEGORIES
from tests.support import warm_market_states


class QuestionSchemaTest(unittest.TestCase):
    def test_sc10_schema_version_is_fixed(self) -> None:
        self.assertEqual(QUESTION_SCHEMA_VERSION, "jev-market-v1")

    def test_question_set_is_fixed(self) -> None:
        self.assertEqual(
            [spec.key for spec in QUESTION_SPECS],
            ["future_return", *PROBABILITY_QUESTIONS],
        )

    def test_future_return_spec(self) -> None:
        spec = QUESTION_SPEC_BY_KEY["future_return"]
        self.assertEqual(spec.kind, "future_return_distribution")
        self.assertEqual(spec.horizons_ms, FUTURE_RETURN_HORIZONS_MS)
        self.assertEqual(spec.horizons_ms, (5_000, 15_000, 30_000, 60_000))
        self.assertEqual(spec.categories, FUTURE_RETURN_CATEGORIES)
        self.assertEqual(spec.probability_sum, 1.0)

    def test_probability_questions_spec(self) -> None:
        for question in PROBABILITY_QUESTIONS:
            with self.subTest(question=question):
                spec = QUESTION_SPEC_BY_KEY[question]
                self.assertEqual(spec.kind, "probability")
                self.assertEqual(spec.horizons_ms, ())
                self.assertEqual(spec.categories, ())

    def test_spec_json_is_auditable(self) -> None:
        encoded = QUESTION_SPEC_BY_KEY["future_return"].as_json()
        self.assertEqual(encoded["horizons"], ["5s", "15s", "30s", "60s"])
        self.assertEqual(encoded["categories"], list(FUTURE_RETURN_CATEGORIES))
        self.assertEqual(encoded["required_probability_sum"], 1.0)
        self.assertEqual(QUESTION_SPEC_BY_KEY["buy_fill_probability"].as_json(), {"key": "buy_fill_probability", "kind": "probability"})

    def test_horizon_key_round_trip(self) -> None:
        for horizon in FUTURE_RETURN_HORIZONS_MS:
            with self.subTest(horizon=horizon):
                self.assertEqual(horizon_from_key(horizon_key(horizon)), horizon)

    def test_invalid_horizon_keys_rejected(self) -> None:
        for key in ("5", "s", "5m", "", "abc", "5.5s"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    horizon_from_key(key)


class CanonicalJsonTest(unittest.TestCase):
    def test_key_order_is_normalized(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_matches_event_store_codec_rule(self) -> None:
        value = {"z": [1.5, 2], "a": {"nested": True}, "m": None}
        self.assertEqual(canonical_json(value), storage_canonical_json(value))

    def test_non_finite_numbers_rejected(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json({"x": float("nan")})


class PredictionRequestBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = warm_market_states(1)[0]

    def test_request_freezes_inputs(self) -> None:
        request = build_prediction_request(self.state, sequence=7, created_at=1_000, ttl_ms=2_000)

        self.assertEqual(request.sequence, 7)
        self.assertEqual(request.request_id, f"{self.state.identity.symbol}-00000007")
        self.assertIs(request.venue, self.state.identity.venue)
        self.assertEqual(request.symbol, self.state.identity.symbol)
        self.assertEqual(request.as_of, self.state.time.as_of_exchange_ts)
        self.assertEqual(request.feature_schema_version, self.state.feature_schema_version)
        self.assertEqual(request.question_schema_version, QUESTION_SCHEMA_VERSION)
        self.assertEqual(request.horizons_ms, FUTURE_RETURN_HORIZONS_MS)
        self.assertEqual(request.created_at, 1_000)
        self.assertEqual(request.expires_at, 3_000)

    def test_payload_is_canonical_json(self) -> None:
        request = build_prediction_request(self.state, sequence=0, created_at=0, ttl_ms=1_000)
        self.assertEqual(request.payload_json, canonical_json(json.loads(request.payload_json)))

    def test_invalid_ttl_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_prediction_request(self.state, sequence=0, created_at=0, ttl_ms=0)

    def test_unknown_question_schema_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_prediction_request(
                self.state,
                sequence=0,
                created_at=0,
                ttl_ms=1_000,
                question_schema_version="jev-market-v2",
            )

    def test_identical_state_and_schema_produce_identical_request_payload(self) -> None:
        # SC-1：同一 MarketState + 同一 schema → 完全相同的 canonical payload 与 hash
        first = build_prediction_request(self.state, sequence=0, created_at=0, ttl_ms=1_000)
        second = build_prediction_request(self.state, sequence=1, created_at=99, ttl_ms=5_000)

        self.assertEqual(first.payload_json, second.payload_json)
        self.assertEqual(first.market_state_hash, second.market_state_hash)
        self.assertNotEqual(first.request_id, second.request_id)

    def test_state_change_changes_payload_and_hash(self) -> None:
        other = warm_market_states(2)[1]
        first = build_prediction_request(self.state, sequence=0, created_at=0, ttl_ms=1_000)
        second = build_prediction_request(other, sequence=0, created_at=0, ttl_ms=1_000)

        self.assertNotEqual(first.market_state_hash, second.market_state_hash)
        self.assertNotEqual(first.payload_json, second.payload_json)


if __name__ == "__main__":
    unittest.main()
