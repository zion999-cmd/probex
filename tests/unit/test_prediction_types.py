"""Prediction Runtime 核心类型：不可变性与失败映射。"""

from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError

from market.events.types import Venue
from prediction.errors import (
    PredictionInvalidResponseError,
    PredictionParseError,
    PredictionProviderError,
    PredictionTimeoutError,
    PredictionTransportError,
)
from prediction.runtime import outcome_for_error
from prediction.types import (
    FUTURE_RETURN_CATEGORIES,
    FutureReturnDistribution,
    InMemoryPredictionArchive,
    Prediction,
    PredictionMode,
    PredictionOutcome,
    PredictionRecord,
    PredictionRequest,
    PredictionResult,
)


def _distribution(horizon_ms: int = 5_000, flat: float = 0.6) -> FutureReturnDistribution:
    remaining = (1.0 - flat) / 4
    return FutureReturnDistribution(
        horizon_ms=horizon_ms,
        strong_down=remaining,
        down=remaining,
        flat=flat,
        up=remaining,
        strong_up=remaining,
    )


def _prediction(**overrides: object) -> Prediction:
    values: dict[str, object] = {
        "future_return": (_distribution(5_000), _distribution(15_000)),
        "buy_adverse_selection": 0.3,
        "sell_adverse_selection": 0.4,
        "buy_fill_probability": 0.5,
        "sell_fill_probability": 0.6,
        "provider_confidence": 0.7,
        "derived_confidence": 0.1,
    }
    values.update(overrides)
    return Prediction(**values)  # type: ignore[arg-type]


def _request(**overrides: object) -> PredictionRequest:
    values: dict[str, object] = {
        "sequence": 0,
        "request_id": "BTCUSDT-00000000",
        "venue": Venue.BINANCE,
        "symbol": "BTCUSDT",
        "as_of": 1_700_000_000_000,
        "market_state_hash": "sha256:" + "a" * 64,
        "feature_schema_version": "market-state-v1",
        "question_schema_version": "jev-market-v1",
        "horizons_ms": (5_000, 15_000, 30_000, 60_000),
        "created_at": 1_700_000_000_000,
        "expires_at": 1_700_000_002_000,
        "payload_json": "{}",
    }
    values.update(overrides)
    return PredictionRequest(**values)  # type: ignore[arg-type]


def _record(**overrides: object) -> PredictionRecord:
    values: dict[str, object] = {
        "request_id": "BTCUSDT-00000000",
        "sequence": 0,
        "market_state_hash": "sha256:" + "a" * 64,
        "feature_schema_version": "market-state-v1",
        "question_schema_version": "jev-market-v1",
        "provider": "fake-jev",
        "model": "fake-model-v1",
        "mode": PredictionMode.LIVE_REQUERY,
        "as_of": 1_700_000_000_000,
        "request_created_at": 1_700_000_000_000,
        "response_received_at": 1_700_000_000_025,
        "latency_ms": 25,
        "expires_at": 1_700_000_002_000,
        "raw_response": "{}",
        "prediction": _prediction(),
    }
    values.update(overrides)
    return PredictionRecord(**values)  # type: ignore[arg-type]


class PredictionRequestTest(unittest.TestCase):
    def test_valid_request(self) -> None:
        request = _request()
        self.assertEqual(request.sequence, 0)
        self.assertEqual(request.horizons_ms, (5_000, 15_000, 30_000, 60_000))

    def test_request_is_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            _request().sequence = 5  # type: ignore[misc]

    def test_invalid_requests_rejected(self) -> None:
        for overrides in (
            {"sequence": -1},
            {"request_id": ""},
            {"market_state_hash": ""},
            {"payload_json": ""},
            {"horizons_ms": ()},
            {"expires_at": 1_699_999_999_999},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    _request(**overrides)


class FutureReturnDistributionTest(unittest.TestCase):
    def test_mapping_follows_schema_order(self) -> None:
        self.assertEqual(
            [category for category, _ in _distribution().as_mapping()],
            list(FUTURE_RETURN_CATEGORIES),
        )

    def test_probability_sum(self) -> None:
        self.assertAlmostEqual(_distribution(flat=0.6).probability_sum, 1.0)

    def test_entropy_of_uniform_distribution_is_max(self) -> None:
        uniform = FutureReturnDistribution(
            horizon_ms=5_000,
            strong_down=0.2,
            down=0.2,
            flat=0.2,
            up=0.2,
            strong_up=0.2,
        )
        self.assertAlmostEqual(uniform.entropy, math.log(len(FUTURE_RETURN_CATEGORIES)))

    def test_entropy_of_certain_distribution_is_zero(self) -> None:
        certain = FutureReturnDistribution(
            horizon_ms=5_000, strong_down=0.0, down=0.0, flat=1.0, up=0.0, strong_up=0.0
        )
        self.assertEqual(certain.entropy, 0.0)

    def test_out_of_range_probability_rejected(self) -> None:
        for value in (-0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    FutureReturnDistribution(
                        horizon_ms=5_000,
                        strong_down=value,
                        down=0.5,
                        flat=0.5,
                        up=0.0,
                        strong_up=0.0,
                    )

    def test_distribution_is_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            _distribution().flat = 0.5  # type: ignore[misc]


class PredictionTest(unittest.TestCase):
    def test_valid_prediction(self) -> None:
        prediction = _prediction()
        self.assertEqual(len(prediction.future_return), 2)
        self.assertEqual(prediction.provider_confidence, 0.7)
        self.assertEqual(prediction.distribution(15_000).horizon_ms, 15_000)  # type: ignore[union-attr]

    def test_missing_horizon_lookup_returns_none(self) -> None:
        self.assertIsNone(_prediction().distribution(60_000))

    def test_provider_confidence_is_optional(self) -> None:
        self.assertIsNone(_prediction(provider_confidence=None).provider_confidence)

    def test_invalid_predictions_rejected(self) -> None:
        cases = (
            {"future_return": ()},
            {"future_return": (_distribution(5_000), _distribution(5_000))},
            {"future_return": (_distribution(15_000), _distribution(5_000))},
            {"buy_adverse_selection": 1.5},
            {"derived_confidence": -0.5},
            {"provider_confidence": 2.0},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    _prediction(**overrides)

    def test_prediction_is_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            _prediction().buy_fill_probability = 0.9  # type: ignore[misc]


class PredictionRecordTest(unittest.TestCase):
    def test_record_is_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            _record().model = "other"  # type: ignore[misc]

    def test_latency_and_timestamps_are_consistent(self) -> None:
        record = _record()
        self.assertEqual(record.latency_ms, record.response_received_at - record.request_created_at)

    def test_expiry_boundary(self) -> None:
        record = _record()
        self.assertFalse(record.is_expired(record.expires_at - 1))
        self.assertFalse(record.is_expired(record.expires_at))
        self.assertTrue(record.is_expired(record.expires_at + 1))

    def test_ttl_remaining(self) -> None:
        record = _record()
        self.assertEqual(record.ttl_remaining_ms(record.request_created_at), 2_000)
        self.assertEqual(record.ttl_remaining_ms(record.expires_at + 500), 0)


class PredictionResultTest(unittest.TestCase):
    def test_accepted_flag(self) -> None:
        accepted = PredictionResult(
            outcome=PredictionOutcome.ACCEPTED,
            request=_request(),
            record=_record(),
            error=None,
            completed_at=0,
        )
        failed = PredictionResult(
            outcome=PredictionOutcome.TIMEOUT,
            request=_request(),
            record=None,
            error=PredictionTimeoutError("late"),
            completed_at=0,
        )
        self.assertTrue(accepted.accepted)
        self.assertFalse(failed.accepted)
        self.assertIsNone(failed.record)


class OutcomeMappingTest(unittest.TestCase):
    def test_each_failure_type_maps_to_its_outcome(self) -> None:
        cases = (
            (PredictionTimeoutError("x"), PredictionOutcome.TIMEOUT),
            (PredictionTransportError("x"), PredictionOutcome.TRANSPORT_ERROR),
            (PredictionProviderError("x"), PredictionOutcome.PROVIDER_ERROR),
            (PredictionParseError("x"), PredictionOutcome.PARSE_ERROR),
            (PredictionInvalidResponseError("x"), PredictionOutcome.INVALID_RESPONSE),
        )
        for error, outcome in cases:
            with self.subTest(error=type(error).__name__):
                self.assertIs(outcome_for_error(error), outcome)


class ArchiveTest(unittest.TestCase):
    def test_store_and_find(self) -> None:
        archive = InMemoryPredictionArchive()
        record = _record()
        archive.store(record)

        self.assertEqual(archive.find(market_state_hash=record.market_state_hash, question_schema_version="jev-market-v1"), record)
        self.assertIsNone(archive.find(market_state_hash="sha256:other", question_schema_version="jev-market-v1"))
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive.records, (record,))

    def test_first_record_wins_for_the_same_state(self) -> None:
        archive = InMemoryPredictionArchive()
        first = _record(sequence=0, model="model-a")
        second = _record(sequence=1, model="model-b")
        archive.store(first)
        archive.store(second)

        self.assertEqual(archive.find(market_state_hash=first.market_state_hash, question_schema_version="jev-market-v1"), first)
        self.assertEqual(len(archive.records), 2)
        self.assertEqual(len(archive), 1)


if __name__ == "__main__":
    unittest.main()
