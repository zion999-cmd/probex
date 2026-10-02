"""P0001.12.2 G1–G5 单测：事实暴露与 UNKNOWN 语义（不新增事实来源）。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from product.account_timeline import AccountSample, BoundedAccountTimeline, project_account_timeline
from product.facts import MAX_TEXT_CHARS, SUPPORTED_KINDS, raw_facts_for
from product.market_projection import MarketProjectionConfig
from product.types import RuntimeMode
from tests.unit.test_product_snapshot import service


def config() -> MarketProjectionConfig:
    return MarketProjectionConfig(window_ms=600_000, bucket_ms=1_000, max_points=10, price_levels=5)


class G1FillEvidenceTest(unittest.TestCase):
    def fill(self, *, client_id: str = "probex-s1-1", trade_id: str = "T-1") -> object:
        return SimpleNamespace(client_order_id=client_id, ts=1_500, price=82_000.0, quantity=0.0007,
                               fee=0.01, trade_id=trade_id)

    def test_fills_are_exposed_when_wired(self) -> None:
        snapshot = service(fills=lambda: (self.fill(),), recent_fill_limit=10).snapshot()
        view = snapshot.execution.recent_fills[0]

        self.assertEqual(view.client_order_id.value, "probex-s1-1")
        self.assertEqual(view.price.value, 82_000.0)
        self.assertEqual(snapshot.execution.recent_fill_limit, 10)

    def test_fills_stage_closes_the_causal_chain(self) -> None:
        snapshot = service(fills=lambda: (self.fill(),), recent_fill_limit=10).snapshot()
        fill_entries = [entry for entry in snapshot.evidence.trace if entry.stage == "fill"]

        self.assertEqual(len(fill_entries), 1)
        self.assertEqual(fill_entries[0].identity.value, "probex-s1-1")

    def test_no_fills_wired_means_not_exposed_not_no_trades(self) -> None:
        snapshot = service().snapshot()
        self.assertEqual(snapshot.execution.recent_fills, ())
        self.assertEqual(snapshot.execution.recent_fill_limit, 0)
        self.assertFalse([e for e in snapshot.evidence.trace if e.stage == "fill"])

    def test_fill_bound_is_respected(self) -> None:
        fills = tuple(self.fill(client_id=f"probex-s1-{i}") for i in range(20))
        snapshot = service(fills=lambda: fills, recent_fill_limit=3).snapshot()
        self.assertEqual([f.client_order_id.value for f in snapshot.execution.recent_fills],
                         ["probex-s1-17", "probex-s1-18", "probex-s1-19"])


class G2RawFactsTest(unittest.TestCase):
    def test_supported_kinds_and_bounded_text(self) -> None:
        order = SimpleNamespace(client_order_id="probex-s1-1", symbol="BTCUSDT", side="buy", status="OPEN",
                                price=1.0, quantity=2.0, filled_quantity=0.0, reduce_only=False,
                                created_at=1, updated_at=2)
        view = raw_facts_for("order", "probex-s1-1", order)

        self.assertTrue(view.available)
        self.assertEqual(view.as_dict()["symbol"].value, "BTCUSDT")
        long_text = raw_facts_for("prediction", "p1", SimpleNamespace(request_id="p1", raw_response="x" * 9_000))
        self.assertIn("raw_response", long_text.truncated_fields)
        self.assertEqual(len(long_text.as_dict()["raw_response"].value), MAX_TEXT_CHARS)

    def test_missing_object_is_unavailable(self) -> None:
        view = raw_facts_for("fill", "nope", None)
        self.assertFalse(view.available)
        self.assertEqual(view.facts, ())
        self.assertTrue(view.notes)

    def test_bad_kind_or_identity_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            raw_facts_for("bogus", "x", None)
        with self.assertRaises(ValueError):
            raw_facts_for("order", "", None)
        self.assertIn("order", SUPPORTED_KINDS)

    def test_service_lookup_is_explicitly_unavailable_when_not_wired(self) -> None:
        # F5：provider 未接线 ⇒ 明确 unavailable（≠ “事实不存在”）
        from product.facts import RawFactProviderUnavailable

        with self.assertRaises(RawFactProviderUnavailable):
            service().raw_facts_view("order", "probex-s1-1")


class G3AccountTimelineTest(unittest.TestCase):
    def test_projection_is_bounded_and_keeps_unknown(self) -> None:
        buffer = BoundedAccountTimeline(capacity=100, run_id="run-1")
        for index in range(20):
            buffer.feed(AccountSample(ts=index * 1_000, equity=1_000.0 + index, balance=9_000.0,
                                      position_qty=None, exposure_total=50.0))
        timeline = project_account_timeline(buffer.samples(), config=config())

        self.assertLessEqual(len(timeline.points), config().max_points)
        self.assertTrue(timeline.truncated)
        self.assertFalse(timeline.points[0].position_qty.known)
        self.assertIsNone(timeline.points[0].position_qty.value)
        self.assertEqual(buffer.counts["capacity"], 100)

    def test_empty_projection_is_explicitly_unknown(self) -> None:
        timeline = project_account_timeline([], config=config())
        self.assertEqual(timeline.points, ())
        self.assertTrue(timeline.notes)

    def test_invalid_capacity_is_refused(self) -> None:
        with self.assertRaises(Exception):
            BoundedAccountTimeline(capacity=0)


class G4HealthFactsTest(unittest.TestCase):
    def test_provider_and_accounting_health_are_exposed(self) -> None:
        snapshot = service(prediction_provider_status=lambda: "DEGRADED",
                           accounting_health=lambda: SimpleNamespace(value="healthy")).snapshot()

        self.assertEqual(snapshot.health.prediction_provider.value, "DEGRADED")
        self.assertEqual(snapshot.health.accounting.value, "healthy")

    def test_unwired_health_is_unknown_not_healthy(self) -> None:
        snapshot = service().snapshot()
        for fact in (snapshot.health.prediction_provider, snapshot.health.accounting):
            with self.subTest(reason=fact.reason):
                self.assertFalse(fact.known)
                self.assertIsNone(fact.value)
                self.assertTrue(fact.reason)


class G5AuthorityFactsTest(unittest.TestCase):
    def authority(self) -> object:
        return SimpleNamespace(kind="NORMAL", issued_at_ms=1_000, expires_at_ms=31_000,
                               recovery_generation="RecoveryGeneration(0, 1)", market_generation=7)

    def test_full_authority_facts_are_exposed(self) -> None:
        snapshot = service(authority=self.authority).snapshot()
        readiness = snapshot.readiness

        self.assertEqual(readiness.authority_kind.value, "NORMAL")
        self.assertEqual(readiness.authority_issued_at_ms.value, 1_000)
        self.assertEqual(readiness.authority_expires_at_ms.value, 31_000)
        self.assertEqual(readiness.authority_recovery_generation.value, "RecoveryGeneration(0, 1)")
        self.assertEqual(readiness.authority_market_generation.value, 7)

    def test_missing_authority_is_unknown(self) -> None:
        readiness = service().snapshot().readiness
        for fact in (readiness.authority_kind, readiness.authority_issued_at_ms,
                     readiness.authority_expires_at_ms, readiness.authority_recovery_generation,
                     readiness.authority_market_generation):
            with self.subTest(reason=fact.reason):
                self.assertFalse(fact.known)
                self.assertTrue(fact.reason)

    def test_authority_never_survives_a_mode_change(self) -> None:
        """authority 是 runtime 事实，与 mode 无关但必须显式：REPLAY 下未接线 ⇒ UNKNOWN。"""
        snapshot = service(identity=__import__("tests.unit.test_product_snapshot", fromlist=["identity"])
                           .identity(RuntimeMode.REPLAY)).snapshot()
        self.assertFalse(snapshot.readiness.authority_kind.known)
