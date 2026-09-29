"""P0001.9.7.2：订单参数 Decimal 归一的 ownership 与行为。"""

from __future__ import annotations

import unittest
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP

from connectors.binance.execution import (
    BinanceExecutionAdapter,
    ExecutionAuthorityContext,
    SubmitClassification,
)
from connectors.binance.market_data.exchange_info import TradingRules
from connectors.binance.execution.adapter import normalizer_from_rules
from execution.normalization import OrderNormalizationError, OrderNormalizer
from execution.types import Order, OrderStatus
from market.events.types import Venue
from portfolio.types import Side
from readiness.authority import issue_authority
from readiness.types import (
    Environment,
    LiveReadinessResult,
    LiveReadinessScope,
    LiveReadinessStatus,
    PrivateLatencyStatus,
    RecoveryGeneration,
)
from risk.types import KillSwitchMode
from tests.execution_support_live import (
    FakeExecutionFetcher, ack_payload, client, client_order_id_from_url, echoing_ack,
)
from tests.support import BASE_TS

NOW = BASE_TS + 1_000


def rules(**overrides: object) -> TradingRules:
    values: dict[str, object] = {
        "symbol": "BTCUSDT", "status": "TRADING", "tick_size": 0.1, "min_price": 100.0,
        "max_price": 1_000_000.0, "step_size": 0.0001, "min_qty": 0.0001, "max_qty": 100.0,
        "min_notional": 50.0,
    }
    values.update(overrides)
    return TradingRules(**values)  # type: ignore[arg-type]


def normalizer(**overrides: object) -> OrderNormalizer:
    values: dict[str, object] = {
        "tick_size": Decimal("0.1"), "step_size": Decimal("0.0001"),
        "price_rounding": ROUND_HALF_UP, "quantity_rounding": ROUND_DOWN,
    }
    values.update(overrides)
    return OrderNormalizer(**values)  # type: ignore[arg-type]


class OrderNormalizerTest(unittest.TestCase):
    def test_float_noise_price_becomes_tick_exact_text(self) -> None:
        """实测缺陷：83958.20000000001 归一后必须是 tick 精确的 '83958.2'。"""
        result = normalizer().normalize(price=83958.20000000001, quantity=0.0007)

        self.assertEqual(result.price_text, "83958.2")
        self.assertEqual(result.quantity_text, "0.0007")
        self.assertTrue(result.adjusted)
        self.assertEqual(result.price, Decimal("83958.2"))

    def test_decimal_is_used_not_binary_float(self) -> None:
        result = normalizer(tick_size=Decimal("0.01")).normalize(price=0.1 + 0.2, quantity=1)

        self.assertEqual(result.price_text, "0.3")

    def test_rounding_modes_are_applied_explicitly(self) -> None:
        down = normalizer(price_rounding=ROUND_DOWN).normalize(price=100.19, quantity=1)
        up = normalizer(price_rounding=ROUND_UP).normalize(price=100.19, quantity=1)

        self.assertEqual(down.price_text, "100.1")
        self.assertEqual(up.price_text, "100.2")

    def test_quantity_rounds_to_step(self) -> None:
        result = normalizer(step_size=Decimal("0.0001"), quantity_rounding=ROUND_DOWN).normalize(
            price=60_000.0, quantity=0.00075
        )

        self.assertEqual(result.quantity_text, "0.0007")

    def test_no_change_is_reported_as_not_adjusted(self) -> None:
        result = normalizer().normalize(price=60_000.0, quantity=0.001)

        self.assertFalse(result.adjusted)
        self.assertEqual((result.price_text, result.quantity_text), ("60000", "0.001"))

    def test_rounding_mode_is_mandatory(self) -> None:
        with self.assertRaises(TypeError):
            OrderNormalizer(tick_size=Decimal("0.1"), step_size=Decimal("0.0001"))  # type: ignore[call-arg]

    def test_invalid_rounding_mode_is_refused(self) -> None:
        with self.assertRaises(OrderNormalizationError):
            normalizer(price_rounding="ROUND_FLOOR")

    def test_non_positive_or_non_finite_inputs_are_refused(self) -> None:
        for price in (0, -1, float("nan"), float("inf")):
            with self.subTest(price=price):
                with self.assertRaises(OrderNormalizationError):
                    normalizer().normalize(price=price, quantity=0.001)

    def test_zero_after_quantization_is_refused(self) -> None:
        with self.assertRaises(OrderNormalizationError):
            normalizer(step_size=Decimal("1"), quantity_rounding=ROUND_DOWN).normalize(
                price=60_000.0, quantity=0.4
            )

    def test_from_rules_uses_exchange_facts(self) -> None:
        built = normalizer_from_rules(rules(), price_rounding=ROUND_HALF_UP,
                                     quantity_rounding=ROUND_DOWN)

        self.assertEqual(built.tick_size, Decimal("0.1"))
        self.assertEqual(built.step_size, Decimal("0.0001"))


def ack_matching_request(url: str) -> dict:
    """按请求回显 clientOrderId / price / origQty（ACK 必须与本地期望一致）。"""
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(url).query)
    payload = ack_payload(client_order_id=query["newClientOrderId"][0], status="NEW")
    payload["price"] = query["price"][0]
    payload["origQty"] = query["quantity"][0]
    return payload


def order(**overrides: object) -> Order:
    values: dict[str, object] = {
        "client_order_id": "probex-s1-000001", "venue": Venue.BINANCE, "symbol": "BTCUSDT",
        "side": Side.BUY, "price": 83_958.20000000001, "quantity": 0.0007,
        "status": OrderStatus.PENDING_CREATE, "created_at": BASE_TS, "updated_at": BASE_TS,
        "post_only": True,
    }
    values.update(overrides)
    return Order(**values)  # type: ignore[arg-type]


def authority():
    return issue_authority(
        LiveReadinessResult(status=LiveReadinessStatus.LIVE_READY,
                            scope=LiveReadinessScope.TESTNET_LIVE_READY, reasons=(),
                            latency_status=PrivateLatencyStatus.HEALTHY),
        provenance=__import__("readiness.authority", fromlist=["ReadinessProvenance"]).ReadinessProvenance(
            recovery_generation=RecoveryGeneration(0, 0), market_generation=1, market_evidence_ts=NOW,
            account_snapshot_ts=NOW, clock_calibration_ts=NOW, hwm_activation_id="a", hwm_generation=1,
            risk_policy_fingerprint="r", evidence_digest="sha256:x"),
        authority_id="auth-1", now_ms=NOW, authority_ttl_ms=60_000, environment=Environment.TESTNET,
    )


def context() -> ExecutionAuthorityContext:
    return ExecutionAuthorityContext(
        authority=authority(), recovery_generation=RecoveryGeneration(0, 0), market_generation=1,
        hwm_activation_id="a", hwm_generation=1, kill_switch_mode=KillSwitchMode.NORMAL,
    )


class AdapterNormalizationOwnershipTest(unittest.TestCase):
    """ownership 的可执行断言：注入 normalizer 才归一；不注入则保持 D-048（不隐式 round）。"""

    def adapter(self, transport, *, normalizer_value):
        return BinanceExecutionAdapter(
            rest=client(transport, now_ms=NOW), environment=Environment.TESTNET,
            authority_provider=context, rules_provider=lambda: rules(symbol="BTCUSDT"),
            symbol="BTCUSDT", normalizer=normalizer_value,
        )

    def test_without_normalizer_float_price_is_sent_unchanged(self) -> None:
        transport = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter = self.adapter(transport, normalizer_value=None)

        adapter.submit_with_outcome(order())

        self.assertIn("83958.20000000001", transport.seen_queries[0])

    def test_with_normalizer_rest_request_carries_tick_exact_price(self) -> None:
        transport = FakeExecutionFetcher(responses={"POST": ack_matching_request})
        adapter = self.adapter(transport, normalizer_value=normalizer())

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_ACCEPTED)
        self.assertIn("price=83958.2", transport.seen_queries[0])
        self.assertNotIn("83958.20000000001", transport.seen_queries[0])
        self.assertIn("quantity=0.0007", transport.seen_queries[0])
        self.assertEqual(client_order_id_from_url(transport.seen_queries[0]), "probex-s1-000001")


if __name__ == "__main__":
    unittest.main()
