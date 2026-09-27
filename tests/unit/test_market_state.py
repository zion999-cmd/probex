"""SC-10：MarketState 不可变性与 feature schema 版本。"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields

from market.features.returns import UNAVAILABLE_RETURNS
from market.features.volatility import UNAVAILABLE_VOLATILITY
from market.state.builder import compute_completeness
from market.state.types import (
    FEATURE_SCHEMA_VERSION,
    DepthFeatures,
    FlowFeatures,
    MarketState,
    PriceFeatures,
    ReturnsFeatures,
    StateTime,
    TradeFeatures,
    VolatilityFeatures,
    MarketIdentity,
)
from market.events.types import Venue
from tests.support import BASE_TS, depth_snapshot_event, feature_engine


class FeatureSchemaVersionTest(unittest.TestCase):
    def test_sc10_schema_version_is_market_state_v1(self) -> None:
        self.assertEqual(FEATURE_SCHEMA_VERSION, "market-state-v1")

    def test_state_carries_schema_version(self) -> None:
        engine = feature_engine()
        state = engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)]))
        self.assertEqual(state.feature_schema_version, FEATURE_SCHEMA_VERSION)

    def test_state_rejects_unknown_schema_version(self) -> None:
        engine = feature_engine()
        state = engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)]))

        with self.assertRaises(ValueError):
            MarketState(
                identity=state.identity,
                time=state.time,
                quality=state.quality,
                price=state.price,
                depth=state.depth,
                flow=state.flow,
                trade=state.trade,
                returns=state.returns,
                volatility=state.volatility,
                feature_schema_version="market-state-v2",
            )


class MarketStateImmutabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = feature_engine()
        self.state = engine.on_market_event(
            depth_snapshot_event(
                100,
                bids=[(100.0, 1.0)],
                asks=[(101.0, 1.0)],
                exchange_ts=BASE_TS,
                receive_ts=BASE_TS + 1,
                process_ts=BASE_TS + 2,
            )
        )

    def test_state_is_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.state.feature_schema_version = "x"  # type: ignore[misc]

    def test_sections_are_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.state.price.mid = 1.0  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            self.state.quality.tradeable = True  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            self.state.flow.book_update_count = 99  # type: ignore[misc]

    def test_sections_are_the_declared_types(self) -> None:
        self.assertIsInstance(self.state.identity, MarketIdentity)
        self.assertIsInstance(self.state.time, StateTime)
        self.assertIsInstance(self.state.price, PriceFeatures)
        self.assertIsInstance(self.state.depth, DepthFeatures)
        self.assertIsInstance(self.state.flow, FlowFeatures)
        self.assertIsInstance(self.state.trade, TradeFeatures)
        self.assertIsInstance(self.state.returns, ReturnsFeatures)
        self.assertIsInstance(self.state.volatility, VolatilityFeatures)

    def test_identity_and_time_fields(self) -> None:
        self.assertIs(self.state.identity.venue, Venue.BINANCE)
        self.assertEqual(self.state.identity.symbol, "BTCUSDT")
        self.assertEqual(self.state.time.as_of_exchange_ts, BASE_TS)
        self.assertEqual(self.state.time.as_of_receive_ts, BASE_TS + 1)
        self.assertEqual(self.state.time.event_ordinal, 0)

    def test_event_ordinal_increments_and_states_remain_distinct(self) -> None:
        engine = feature_engine()
        first = engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)]))
        second = engine.on_market_event(depth_snapshot_event(110, bids=[(100.0, 2.0)], asks=[(101.0, 1.0)]))

        self.assertEqual(first.time.event_ordinal, 0)
        self.assertEqual(second.time.event_ordinal, 1)
        self.assertNotEqual(first, second)
        # 早先的状态不会被后续事件改写
        self.assertEqual(first.price.bid_size, 1.0)

    def test_equality_is_structural(self) -> None:
        event = depth_snapshot_event(100, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])
        first = feature_engine().on_market_event(event)
        second = feature_engine().on_market_event(event)
        self.assertEqual(first, second)

    def test_same_engine_states_differ_by_ordinal(self) -> None:
        engine = feature_engine()
        event = depth_snapshot_event(100, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])
        self.assertNotEqual(engine.on_market_event(event), engine.on_market_event(event))


class CompletenessTest(unittest.TestCase):
    def _sections(self, **overrides: object):
        base = {
            "price": PriceFeatures(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0),
            "depth": DepthFeatures(*([1.0] * 14)),
            "flow": FlowFeatures(*([1.0] * 7), 1, 1, 1, 1, 1),
            "returns": ReturnsFeatures(*([0.1] * 7)),
            "volatility": VolatilityFeatures(*([0.2] * 4)),
        }
        base.update(overrides)
        return base

    def test_all_available_is_one(self) -> None:
        self.assertEqual(compute_completeness(**self._sections()), 1.0)

    def test_unavailable_returns_and_volatility_reduce_completeness(self) -> None:
        sections = self._sections(returns=UNAVAILABLE_RETURNS, volatility=UNAVAILABLE_VOLATILITY)
        self.assertEqual(compute_completeness(**sections), 34 / 45)

    def test_slot_total_is_declared_feature_fields(self) -> None:
        total = sum(
            len(fields(section))
            for section in (PriceFeatures, DepthFeatures, FlowFeatures, ReturnsFeatures, VolatilityFeatures)
        )
        self.assertEqual(total, 45)


if __name__ == "__main__":
    unittest.main()
