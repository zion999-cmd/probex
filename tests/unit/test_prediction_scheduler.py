"""资格闸门与并发/迟到判定（同步单元测试）。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from market.events.types import Venue
from market.health.state import BookHealth
from prediction.scheduler import Eligibility, EligibilityReason, PredictionScheduler, check_eligibility
from prediction.types import PredictionMode, PredictionOutcome, PredictionRecord, PredictionRequest
from tests.support import warm_market_states


def _state():
    return warm_market_states(1)[0]


def _request(sequence: int = 0) -> PredictionRequest:
    return PredictionRequest(
        sequence=sequence,
        request_id=f"BTCUSDT-{sequence:08d}",
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        as_of=1,
        market_state_hash="sha256:" + "a" * 64,
        feature_schema_version="market-state-v1",
        question_schema_version="jev-market-v1",
        horizons_ms=(5_000,),
        created_at=0,
        expires_at=1_000,
        payload_json="{}",
    )


def _record(sequence: int = 0) -> PredictionRecord:
    from prediction.types import FutureReturnDistribution, Prediction

    distribution = FutureReturnDistribution(
        horizon_ms=5_000, strong_down=0.2, down=0.2, flat=0.2, up=0.2, strong_up=0.2
    )
    return PredictionRecord(
        request_id=f"BTCUSDT-{sequence:08d}",
        sequence=sequence,
        market_state_hash="sha256:" + "a" * 64,
        feature_schema_version="market-state-v1",
        question_schema_version="jev-market-v1",
        provider="fake-jev",
        model="fake-model-v1",
        mode=PredictionMode.LIVE_REQUERY,
        as_of=1,
        request_created_at=0,
        response_received_at=10,
        latency_ms=10,
        expires_at=1_000,
        raw_response="{}",
        prediction=Prediction(
            future_return=(distribution,),
            buy_adverse_selection=0.5,
            sell_adverse_selection=0.5,
            buy_fill_probability=0.5,
            sell_fill_probability=0.5,
            provider_confidence=None,
            derived_confidence=0.0,
        ),
    )


class EligibilityTest(unittest.TestCase):
    def test_warm_state_is_eligible(self) -> None:
        eligibility = check_eligibility(_state())
        self.assertTrue(eligibility.eligible)
        self.assertIs(eligibility.reason, EligibilityReason.ELIGIBLE)

    def test_unhealthy_book_is_not_eligible(self) -> None:
        state = _state()
        broken = replace(state, quality=replace(state.quality, book_health=BookHealth.STALE))
        eligibility = check_eligibility(broken)
        self.assertFalse(eligibility.eligible)
        self.assertIs(eligibility.reason, EligibilityReason.BOOK_NOT_HEALTHY)

    def test_history_not_ready_is_not_eligible(self) -> None:
        state = _state()
        cold = replace(state, quality=replace(state.quality, history_ready=False, feature_ready=False, tradeable=False))
        eligibility = check_eligibility(cold)
        self.assertFalse(eligibility.eligible)
        self.assertIs(eligibility.reason, EligibilityReason.NOT_HISTORY_READY)

    def test_invalid_age_is_not_eligible(self) -> None:
        state = _state()
        aged = replace(state, quality=replace(state.quality, age_valid=False, tradeable=False))
        eligibility = check_eligibility(aged)
        self.assertFalse(eligibility.eligible)
        self.assertIs(eligibility.reason, EligibilityReason.AGE_INVALID)

    def test_eligibility_matches_tradeable_flag(self) -> None:
        state = _state()
        variants = (
            state,
            replace(
                state,
                quality=replace(
                    state.quality, book_health=BookHealth.RESYNCING, feature_ready=False, tradeable=False
                ),
            ),
            replace(
                state,
                quality=replace(state.quality, history_ready=False, feature_ready=False, tradeable=False),
            ),
            replace(state, quality=replace(state.quality, age_valid=False, tradeable=False)),
        )
        for variant in variants:
            with self.subTest(tradeable=variant.quality.tradeable):
                self.assertEqual(check_eligibility(variant).eligible, variant.quality.tradeable)

    def test_eligibility_ignores_sequence_contiguity(self) -> None:
        state = _state()
        stale_stream = replace(state, quality=replace(state.quality, sequence_contiguous=False))
        self.assertTrue(check_eligibility(stale_stream).eligible)


class SchedulerInflightTest(unittest.TestCase):
    def test_single_flight_by_default(self) -> None:
        scheduler = PredictionScheduler()
        self.assertEqual(scheduler.max_inflight, 1)
        self.assertEqual(scheduler.reserve(), 0)
        self.assertIsNone(scheduler.reserve())
        self.assertEqual(scheduler.inflight, 1)

    def test_abandon_frees_the_slot(self) -> None:
        scheduler = PredictionScheduler()
        scheduler.reserve()
        scheduler.abandon()

        self.assertEqual(scheduler.inflight, 0)
        self.assertEqual(scheduler.reserve(), 1)

    def test_sequence_is_monotonic(self) -> None:
        scheduler = PredictionScheduler(max_inflight=3)
        self.assertEqual([scheduler.reserve() for _ in range(3)], [0, 1, 2])

    def test_accept_frees_the_slot(self) -> None:
        scheduler = PredictionScheduler()
        request = _request()
        scheduler.reserve()
        outcome = scheduler.accept(request=request, record=_record())
        self.assertIs(outcome, PredictionOutcome.ACCEPTED)
        self.assertEqual(scheduler.inflight, 0)

    def test_invalid_max_inflight_rejected(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PredictionScheduler(max_inflight=value)

    def test_completion_without_inflight_raises(self) -> None:
        scheduler = PredictionScheduler()
        with self.assertRaises(RuntimeError):
            scheduler.accept(request=_request(), record=_record())
        with self.assertRaises(RuntimeError):
            scheduler.abandon()


class SchedulerStalenessTest(unittest.TestCase):
    def test_latest_accepted_moves_forward(self) -> None:
        scheduler = PredictionScheduler(max_inflight=2)
        first, second = _request(0), _request(1)
        scheduler.reserve()
        scheduler.reserve()

        self.assertIs(scheduler.accept(request=first, record=_record(0)), PredictionOutcome.ACCEPTED)
        self.assertIs(scheduler.accept(request=second, record=_record(1)), PredictionOutcome.ACCEPTED)
        self.assertEqual(scheduler.latest_accepted.sequence, 1)  # type: ignore[union-attr]
        self.assertEqual(scheduler.accepted_count, 2)

    def test_late_older_response_is_stale(self) -> None:
        scheduler = PredictionScheduler(max_inflight=2)
        older, newer = _request(0), _request(1)
        scheduler.reserve()
        scheduler.reserve()

        scheduler.accept(request=newer, record=_record(1))
        outcome = scheduler.accept(request=older, record=_record(0))

        self.assertIs(outcome, PredictionOutcome.STALE_RESPONSE)
        self.assertEqual(scheduler.latest_accepted.sequence, 1)  # type: ignore[union-attr]
        self.assertEqual(scheduler.stale_count, 1)
        self.assertEqual(scheduler.accepted_count, 1)

    def test_stale_response_does_not_release_into_latest(self) -> None:
        scheduler = PredictionScheduler(max_inflight=2)
        scheduler.reserve()
        scheduler.reserve()
        scheduler.accept(request=_request(1), record=_record(1))
        scheduler.accept(request=_request(0), record=_record(0))

        self.assertEqual(scheduler.latest_sequence, 1)
        self.assertEqual(scheduler.inflight, 0)

    def test_replayed_record_becomes_current(self) -> None:
        scheduler = PredictionScheduler()
        scheduler.record_replayed(_record(4))

        self.assertEqual(scheduler.latest_sequence, 4)
        self.assertEqual(scheduler.latest_accepted.sequence, 4)  # type: ignore[union-attr]
        self.assertEqual(scheduler.inflight, 0)

    def test_replayed_record_never_moves_sequence_backwards(self) -> None:
        scheduler = PredictionScheduler()
        scheduler.record_replayed(_record(5))
        scheduler.record_replayed(_record(2))

        self.assertEqual(scheduler.latest_sequence, 5)


if __name__ == "__main__":
    unittest.main()
