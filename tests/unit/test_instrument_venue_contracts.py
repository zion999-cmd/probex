"""P0001.15 单元验收：Instrument Domain / Venue contracts / ReferencePrice / correlation。

覆盖 SC-3、SC-4、SC-5、SC-10、SC-11、SC-12、SC-19、§8（三分类）、§15（correlation）。
"""

from __future__ import annotations

import unittest

from domain.instruments import (AssetClass, InstrumentCapabilities, InstrumentError, InstrumentRegistry,
                                PriceType, ProductType, ReferencePricePolicy, SettlementType,
                                TradingSessionType, instrument_id_for, perpetual_crypto_spec)
from execution.events import ExecutionEventType, OrderAccepted, OrderRejected
from execution.tracker import OrderTracker
from market.events.types import Venue
from portfolio.types import Side
from risk.types import InvalidOrderProposalError, OrderCorrelation, OrderProposal
from venue import (ConnectionState, MarkPriceReferenceSource, ReferencePriceProvider, VenueEnvironment,
                   VenueError, VenueIdentity, VenueType, classify_submission, paper_venue,
                   venue_for_mode)
from venue.health import ConnectorHealth, MarketConnectorHealth, PrivateConnectorHealth


def spec(**overrides: object):
    values: dict[str, object] = {
        "instrument_id": "paper:BTCUSDT", "symbol": "BTCUSDT", "base_asset": "BTC",
        "quote_asset": "USDT", "settlement_asset": "USDT", "price_tick": 0.1,
        "quantity_step": 0.0001, "min_quantity": 0.0001, "min_notional": 50.0,
    }
    values.update(overrides)
    return perpetual_crypto_spec(**values)          # type: ignore[arg-type]


class InstrumentDomainTest(unittest.TestCase):
    def test_sc1_sc2_perpetual_identity_and_capabilities(self) -> None:
        instrument = spec()

        self.assertEqual(instrument.asset_class, AssetClass.CRYPTO)
        self.assertEqual(instrument.product_type, ProductType.PERPETUAL)
        self.assertEqual((instrument.base_asset, instrument.quote_asset, instrument.settlement_asset),
                         ("BTC", "USDT", "USDT"))
        self.assertTrue(instrument.production_ready)
        view = instrument.capabilities.view()
        self.assertTrue(view["supports_long"] and view["supports_short"])
        self.assertTrue(view["supports_margin"] and view["supports_reduce_only"])
        self.assertFalse(view["has_expiry"])
        self.assertTrue(view["has_funding"])
        self.assertFalse(view["has_corporate_actions"])
        self.assertEqual(view["trading_session_type"], TradingSessionType.CONTINUOUS.value)
        self.assertEqual(view["settlement_type"], SettlementType.PERPETUAL_FUNDING.value)

    def test_sc19_equity_and_future_are_vocabulary_only(self) -> None:
        """EQUITY / FUTURE 只是 vocabulary：不得被当成有生产实现的产品。"""
        from domain.instruments import PRODUCTION_ASSET_CLASSES, PRODUCTION_PRODUCT_TYPES

        self.assertEqual(PRODUCTION_ASSET_CLASSES, frozenset({AssetClass.CRYPTO}))
        self.assertEqual(PRODUCTION_PRODUCT_TYPES, frozenset({ProductType.PERPETUAL}))
        self.assertIn(AssetClass.EQUITY, set(AssetClass))
        self.assertIn(ProductType.DELIVERY_FUTURE, set(ProductType))
        # 结构性约束：EQUITY 不能是 PERPETUAL
        from domain.instruments import InstrumentSpec

        with self.assertRaises(InstrumentError):
            InstrumentSpec(
                instrument_id="ibkr:AAPL", symbol="AAPL", asset_class=AssetClass.EQUITY,
                product_type=ProductType.PERPETUAL, base_asset="AAPL", quote_asset="USD",
                settlement_asset="USD", price_tick=0.01, quantity_step=1.0, min_quantity=1.0,
                min_notional=1.0, capabilities=spec().capabilities)

    def test_perpetual_cannot_have_expiry_and_must_have_funding(self) -> None:
        base = spec().capabilities
        import dataclasses

        with self.assertRaises(InstrumentError):
            spec(capabilities=dataclasses.replace(base, has_expiry=True))
        with self.assertRaises(InstrumentError):
            spec(capabilities=dataclasses.replace(base, has_funding=False))

    def test_declared_parameters_are_validated(self) -> None:
        for field in ("price_tick", "quantity_step", "min_quantity", "min_notional"):
            with self.subTest(field=field):
                with self.assertRaises(InstrumentError):
                    spec(**{field: 0.0})

    def test_sc3_semantics_and_venue_rules_are_separate(self) -> None:
        """instrument semantics（本域）与 venue rules（TradingRules）分离：只读比对，不互相覆盖。"""
        instrument = spec()

        class Rules:
            tick_size = 0.1
            step_size = 0.0001
            min_qty = 0.0001
            min_notional = 50.0

        self.assertEqual(instrument.venue_rules_discrepancies(Rules()), ())
        Rules.tick_size = 0.5
        discrepancies = instrument.venue_rules_discrepancies(Rules())
        self.assertEqual(len(discrepancies), 1)
        self.assertIn("price_tick", discrepancies[0])
        self.assertEqual(instrument.price_tick, 0.1)         # instrument 声明未被覆盖

    def test_registry_resolves_and_refuses_to_guess(self) -> None:
        registry = InstrumentRegistry(instruments=(spec(),), current_id="paper:BTCUSDT")

        self.assertEqual(registry.current.symbol, "BTCUSDT")
        self.assertEqual(registry.by_symbol("BTCUSDT").instrument_id, "paper:BTCUSDT")
        self.assertIsNone(registry.optional("ETHUSDT"))
        with self.assertRaises(InstrumentError):
            registry.by_symbol("ETHUSDT")
        with self.assertRaises(InstrumentError):
            InstrumentRegistry(instruments=(spec(), spec()), current_id="paper:BTCUSDT")
        with self.assertRaises(InstrumentError):
            InstrumentRegistry(instruments=(spec(),), current_id="other:BTCUSDT")

    def test_instrument_id_carries_venue_ownership(self) -> None:
        self.assertEqual(instrument_id_for("BTCUSDT", venue_id="paper"), "paper:BTCUSDT")
        with self.assertRaises(InstrumentError):
            instrument_id_for("", venue_id="paper")


class ReferencePricePolicyTest(unittest.TestCase):
    def test_sc11_last_is_never_a_risk_price_type(self) -> None:
        with self.assertRaises(InstrumentError):
            ReferencePricePolicy(risk_price_types=(PriceType.LAST,))

    def test_policy_exposes_observable_but_not_substitutable_types(self) -> None:
        policy = spec().reference_price_policy

        self.assertEqual(policy.risk_price_types, (PriceType.MARK,))
        self.assertIn(PriceType.MID, policy.observable_price_types)
        self.assertIn(PriceType.INDEX, policy.observable_price_types)
        self.assertFalse(policy.allows_risk_substitution)
        self.assertTrue(policy.accepts_for_risk(PriceType.MARK))
        self.assertFalse(policy.accepts_for_risk(PriceType.LAST))


class VenueIdentityTest(unittest.TestCase):
    def test_venue_and_environment_are_orthogonal(self) -> None:
        identity = VenueIdentity(venue_id="binance", venue_type=VenueType.BINANCE,
                                 environment=VenueEnvironment.TESTNET)
        view = identity.view()

        self.assertEqual(view["venue_id"], "binance")
        self.assertEqual(view["venue_type"], "binance")
        self.assertEqual(view["environment"], "TESTNET")

    def test_invalid_combinations_are_rejected(self) -> None:
        with self.assertRaises(VenueError):
            VenueIdentity(venue_id="paper", venue_type=VenueType.PAPER, environment=VenueEnvironment.LIVE)
        with self.assertRaises(VenueError):
            VenueIdentity(venue_id="binance", venue_type=VenueType.BINANCE,
                          environment=VenueEnvironment.PAPER)
        with self.assertRaises(VenueError):
            VenueIdentity(venue_id="", venue_type=VenueType.PAPER, environment=VenueEnvironment.PAPER)

    def test_mode_maps_to_venue(self) -> None:
        from product.types import RuntimeMode

        self.assertIs(venue_for_mode(RuntimeMode.REPLAY).venue_type, VenueType.PAPER)
        self.assertIs(venue_for_mode(RuntimeMode.PAPER).venue_type, VenueType.PAPER)
        testnet = venue_for_mode(RuntimeMode.TESTNET)
        self.assertIs(testnet.venue_type, VenueType.BINANCE)
        self.assertIs(testnet.environment, VenueEnvironment.TESTNET)
        self.assertIs(venue_for_mode(RuntimeMode.LIVE).environment, VenueEnvironment.LIVE)


class ReferencePriceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MarkPriceReferenceSource(venue_identity=paper_venue(), instrument_id="paper:BTCUSDT")

    def test_sc10_known_mark_carries_price_type_source_and_freshness(self) -> None:
        from tests.support import mark_price_event

        self.assertTrue(self.source.on_market_event(mark_price_event(price=61_000.0, exchange_ts=1_000)))
        reference = self.source.latest(now_ms=1_500)

        self.assertTrue(reference.known)
        self.assertEqual(reference.price, 61_000.0)
        self.assertEqual(reference.price_type, PriceType.MARK)
        self.assertEqual(reference.as_of, 1_000)
        self.assertEqual(reference.freshness_ms, 500)
        self.assertIn("mark_price", reference.source)

    def test_missing_mark_is_unknown_with_reason(self) -> None:
        reference = self.source.latest(now_ms=1_000)

        self.assertFalse(reference.known)
        self.assertIsNone(reference.price)
        self.assertIsNone(reference.as_of)
        self.assertEqual(reference.reason, "REFERENCE_PRICE_MARK_UNAVAILABLE")

    def test_only_mark_price_events_advance_the_source(self) -> None:
        from market.events.payloads import AggressorSide
        from tests.support import trade_event

        self.assertFalse(self.source.on_market_event(
            trade_event(1, price=61_000.0, quantity=1.0, aggressor=AggressorSide.BUY)))
        self.assertEqual(self.source.observation_count, 0)
        self.assertFalse(self.source.latest(now_ms=1_000).known)

    def test_provider_requires_a_registered_source(self) -> None:
        provider = ReferencePriceProvider(venue_identity=paper_venue())

        reference = provider.for_risk(spec(), now_ms=1_000)
        self.assertFalse(reference.known)
        self.assertEqual(reference.reason, "REFERENCE_PRICE_SOURCE_NOT_CONFIGURED")

    def test_provider_observes_market_events_and_serves_risk(self) -> None:
        from tests.support import mark_price_event

        provider = ReferencePriceProvider(venue_identity=paper_venue())
        provider.register("paper:BTCUSDT", PriceType.MARK, self.source)

        self.assertEqual(provider.observe_market_event(mark_price_event(price=60_500.0, exchange_ts=2_000)), 1)
        reference = provider.for_risk(spec(), now_ms=2_100)
        self.assertTrue(reference.known)
        self.assertEqual(reference.price, 60_500.0)


class ConnectorHealthTest(unittest.TestCase):
    def test_sc12_sc28_health_is_split_and_diverges(self) -> None:
        market = MarketConnectorHealth(venue_id="binance", connector_id="binance:market",
                                       connection_state=ConnectionState.CONNECTED,
                                       last_market_event_ms=1_000, event_age_ms=10,
                                       events_observed=True)
        private = PrivateConnectorHealth(venue_id="binance", connector_id="binance:execution",
                                         connection_state=ConnectionState.UNKNOWN)
        both = ConnectorHealth(market=market, private=private)

        self.assertTrue(both.diverges)
        view = both.view()
        self.assertIn("market", view)
        self.assertIn("private", view)
        self.assertNotIn("connected", view)

    def test_unobserved_private_events_stay_unknown(self) -> None:
        """§18：没有 private 事件时不得推断"没有成交"。"""
        health = PrivateConnectorHealth(venue_id="binance", connection_state=ConnectionState.CONNECTED,
                                        last_private_event_ms=5_000, private_event_age_ms=1,
                                        private_events_observed=False)

        self.assertIsNone(health.last_private_event_ms)
        self.assertIsNone(health.private_event_age_ms)
        self.assertFalse(health.private_events_observed)

    def test_market_health_drops_event_facts_when_unobserved(self) -> None:
        health = MarketConnectorHealth(venue_id="paper", connection_state=ConnectionState.CONNECTED,
                                      last_market_event_ms=1, event_age_ms=2, events_observed=False)

        self.assertIsNone(health.last_market_event_ms)
        self.assertIsNone(health.event_age_ms)


class SubmissionClassificationTest(unittest.TestCase):
    def test_three_way_mapping_uses_existing_event_vocabulary(self) -> None:
        accepted = (OrderAccepted(client_order_id="c1", exchange_order_id="x1", timestamp=1),)
        rejected = (OrderRejected(client_order_id="c1", reason="post_only_cross", timestamp=1),)

        self.assertEqual(classify_submission(accepted).value, "ACCEPTED")
        self.assertEqual(classify_submission(rejected).value, "REJECTED")
        self.assertEqual(classify_submission(()).value, "UNKNOWN")            # 无事件 ⇒ 不得 retry
        self.assertEqual(classify_submission(rejected + accepted).value, "REJECTED")


class OrderCorrelationTest(unittest.TestCase):
    def test_correlation_is_validated_and_carried_by_the_order(self) -> None:
        correlation = OrderCorrelation(decision_id="d-1", instrument_id="paper:BTCUSDT", venue_id="paper",
                                       prediction_id="pred-1", market_state_hash="sha256:abc")
        proposal = OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=1.0, price=100.0,
                                 post_only=True, correlation=correlation)
        tracker = OrderTracker(session_id="t", venue=Venue.BINANCE)

        order = tracker.create(proposal, timestamp=1_000)

        self.assertIs(order.correlation, correlation)
        self.assertEqual(tracker.orders_for_decision("d-1"), (order,))
        self.assertEqual(tracker.orders_for_decision("other"), ())

    def test_missing_decision_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidOrderProposalError):
            OrderCorrelation(decision_id="")
        with self.assertRaises(InvalidOrderProposalError):
            OrderCorrelation(decision_id="d-1", venue_id="")

    def test_orders_without_correlation_are_not_linked(self) -> None:
        tracker = OrderTracker(session_id="t", venue=Venue.BINANCE)
        proposal = OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=1.0, price=100.0, post_only=True)

        order = tracker.create(proposal, timestamp=1_000)

        self.assertIsNone(order.correlation)
        self.assertEqual(tracker.orders_for_decision("d-1"), ())
        self.assertEqual(tracker.orders_for_decision("d-1"), ())               # 无 correlation ⇒ 不关联


if __name__ == "__main__":
    unittest.main()
