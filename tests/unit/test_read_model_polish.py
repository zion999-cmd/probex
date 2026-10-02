"""Closure polish（F1/F2/F4/F5）读模型映射与 typed-provider 守卫。"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from market.health.state import BookHealth
from portfolio.accounting import AccountingCore
from portfolio.types import Fill, Side
from product.service import ProductService
from product.types import Fact, RuntimeIdentity, RuntimeMode
from runtime.accounting_facts import AccountingFactsProvider


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(mode=RuntimeMode.PAPER, environment="local", venue="binance",
                           symbol="BTCUSDT", runtime_id="rt-polish", started_at=1_000,
                           data_timestamp=Fact.unknown("no data"))


class F1PositionMappingTest(unittest.TestCase):
    def _provider(self, accounting: object) -> AccountingFactsProvider:
        return AccountingFactsProvider(accounting=accounting, symbol="BTCUSDT", clock=lambda: 1_000)

    def test_flat_position_is_known_zero_not_false(self) -> None:
        facts = self._provider(AccountingCore(initial_balance=1_000.0)).facts()
        self.assertTrue(facts.position_known.known)
        self.assertTrue(facts.position_qty.known)
        self.assertEqual(facts.position_qty.value, 0.0)

    def test_open_position_is_known_positive(self) -> None:
        accounting = AccountingCore(initial_balance=10_000.0)
        accounting.record_fill(Fill(fill_id="f1", order_id="probex-1", venue=__import__(
            "market.events.types", fromlist=["Venue"]).Venue.BINANCE, symbol="BTCUSDT", side=Side.BUY,
            price=100.0, quantity=2.0, fee=0.1, fee_asset="USDT", trade_id="t1",
            exchange_ts=1_000, receive_ts=1_000))
        facts = self._provider(accounting).facts()
        self.assertTrue(facts.position_known.known)
        self.assertEqual(facts.position_qty.value, 2.0)

    def test_unknown_position_never_fakes_zero(self) -> None:
        facts = self._provider(SimpleNamespace()).facts()   # 没有 position 方法
        self.assertFalse(facts.position_known.known)
        self.assertFalse(facts.position_qty.known)
        self.assertIsNone(facts.position_qty.value)

    def test_service_consumes_typed_facts_and_serializes(self) -> None:
        facts = self._provider(AccountingCore(initial_balance=1_000.0)).facts()
        service = ProductService(identity=_identity(), accounting_facts=lambda: facts)
        snap = service.snapshot()
        self.assertTrue(snap.portfolio.position_qty.known)
        self.assertEqual(snap.portfolio.position_qty.value, 0.0)
        self.assertIsNot(snap.portfolio.position_qty.value, False)
        from product.serialization import snapshot_to_jsonable

        payload = snapshot_to_jsonable(snap)["portfolio"]["position_qty"]
        self.assertEqual(payload, {"known": True, "value": 0.0, "reason": None})

    def test_unknown_position_serializes_as_unknown_with_reason(self) -> None:
        facts = self._provider(SimpleNamespace()).facts()
        snap = ProductService(identity=_identity(), accounting_facts=lambda: facts).snapshot()
        self.assertFalse(snap.portfolio.position_qty.known)
        self.assertTrue(snap.portfolio.position_qty.reason)


class F2MarketHealthMappingTest(unittest.TestCase):
    def _market_state(self, *, book_health: object, tradeable: bool) -> object:
        return SimpleNamespace(
            quality=SimpleNamespace(book_health=book_health, book_age_ms=12, tradeable=tradeable),
            price=SimpleNamespace(best_bid=100.0, best_ask=101.0, spread_bps=1.0),
            identity=SimpleNamespace(state_hash="ms-1"))

    def _service(self, state: object) -> ProductService:
        return ProductService(identity=_identity(), market_state=lambda: state)

    def test_healthy_book_is_healthy(self) -> None:
        snap = self._service(self._market_state(book_health=BookHealth.HEALTHY, tradeable=True)).snapshot()
        self.assertTrue(snap.market.healthy.known)
        self.assertTrue(snap.market.healthy.value)
        self.assertEqual(snap.market.book_health.value, "healthy")
        self.assertEqual(snap.market.book_age_ms.value, 12)
        self.assertTrue(snap.health.market_healthy.value)   # 同一 projection

    def test_stale_book_is_not_healthy_even_if_tradeable_flag_absent(self) -> None:
        snap = self._service(self._market_state(book_health=BookHealth.STALE, tradeable=False)).snapshot()
        self.assertTrue(snap.market.healthy.known)
        self.assertFalse(snap.market.healthy.value)          # 不用 tradeable 反推
        self.assertEqual(snap.market.book_health.value, "stale")
        self.assertEqual(snap.health.book_health.value, "stale")

    def test_tradeable_is_independent_of_health(self) -> None:
        snap = self._service(self._market_state(book_health=BookHealth.HEALTHY, tradeable=False)).snapshot()
        self.assertTrue(snap.market.healthy.value)
        self.assertFalse(snap.market.tradeable.value)

    def test_unknown_health_stays_unknown(self) -> None:
        snap = self._service(self._market_state(book_health=None, tradeable=False)).snapshot()
        self.assertFalse(snap.market.healthy.known)
        self.assertFalse(snap.market.book_health.known)
        self.assertFalse(snap.health.market_healthy.known)
        self.assertFalse(snap.market.tradeable.known is False)   # tradeable 仍是已知事实


class F4RunSummaryTest(unittest.TestCase):
    def test_run_summary_view_routes_by_run_id(self) -> None:
        calls: list[str] = []
        service = ProductService(
            identity=_identity(),
            run_summary=lambda: {"run": {"run_id": "latest"}},
            durable_run_summary=lambda run_id: calls.append(run_id) or {"run": {"run_id": run_id}})
        self.assertEqual(service.run_summary_view()["run"]["run_id"], "latest")
        self.assertEqual(service.run_summary_view("run-7")["run"]["run_id"], "run-7")
        self.assertEqual(calls, ["run-7"])


class F5RawFactsProviderTest(unittest.TestCase):
    def test_unwired_provider_is_explicitly_unavailable(self) -> None:
        from product.facts import RawFactProviderUnavailable

        with self.assertRaises(RawFactProviderUnavailable):
            ProductService(identity=_identity()).raw_facts_view("order", "x")

    def test_wired_provider_returns_fact_or_not_found(self) -> None:
        order = SimpleNamespace(client_order_id="probex-1", symbol="BTCUSDT", side="buy",
                                status="OPEN", price=100.0, quantity=1.0, filled_quantity=0.0,
                                reduce_only=False, created_at=1, updated_at=1)
        service = ProductService(identity=_identity(),
                                 raw_fact_lookup=lambda kind, identity: order if identity == "probex-1" else None)
        view = service.raw_facts_view("order", "probex-1")
        self.assertTrue(view.available)
        self.assertIn("client_order_id", [name for name, _ in view.facts])
        self.assertFalse(service.raw_facts_view("order", "missing").available)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
