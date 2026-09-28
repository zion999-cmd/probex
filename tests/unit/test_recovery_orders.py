"""P0001.9.3 单元测试：外部订单/成交归一化与 ownership boundary。"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from portfolio.types import Side

from connectors.binance.private.errors import PrivateFormatError
from connectors.binance.private.orders import (
    classify_orders,
    is_probex_order,
    latest_by_client_order_id,
    parse_external_orders,
)
from connectors.binance.private.trades import parse_external_fills
from tests.private_support import SYMBOL, binance_order_payload, binance_trade_payload


class OrderNormalizationTest(unittest.TestCase):
    def test_order_is_normalized_to_external_order(self) -> None:
        raw = [binance_order_payload(status="PARTIALLY_FILLED", executed_qty="0.001", avg_price="60000.0")]

        orders = parse_external_orders(raw, symbol=SYMBOL)

        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order.client_order_id, "probex-s1-000001")
        self.assertEqual(order.exchange_order_id, "101")
        self.assertIs(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(order.filled_quantity, 0.001)
        self.assertAlmostEqual(order.avg_fill_price, 60000.0)
        self.assertIs(order.side, Side.BUY)
        self.assertAlmostEqual(order.quantity, 0.002)
        self.assertAlmostEqual(order.price, 60000.0)

    def test_all_binance_statuses_map_explicitly(self) -> None:
        expected = {
            "NEW": OrderStatus.OPEN,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "EXPIRED": OrderStatus.EXPIRED,
            "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
            "REJECTED": OrderStatus.FAILED,
        }
        for status, local in expected.items():
            raw = [binance_order_payload(status=status, executed_qty="0.001" if status in
                                        {"PARTIALLY_FILLED", "FILLED"} else "0.000",
                                        avg_price="60000.0" if status in {"PARTIALLY_FILLED", "FILLED"} else "0.0")]
            with self.subTest(status=status):
                self.assertIs(parse_external_orders(raw, symbol=SYMBOL)[0].status, local)

    def test_unknown_status_fails_closed(self) -> None:
        with self.assertRaises(PrivateFormatError):
            parse_external_orders([binance_order_payload(status="NEW_INSURANCE")], symbol=SYMBOL)

    def test_missing_fields_and_symbol_mismatch_fail_closed(self) -> None:
        for mutate in ("drop_client_id", "drop_qty", "wrong_symbol"):
            raw = binance_order_payload()
            if mutate == "drop_client_id":
                del raw["clientOrderId"]
            elif mutate == "drop_qty":
                del raw["origQty"]
            else:
                raw["symbol"] = "ETHUSDT"
            with self.subTest(mutate=mutate):
                with self.assertRaises(PrivateFormatError):
                    parse_external_orders([raw], symbol=SYMBOL)

    def test_filled_quantity_without_avg_price_fails_closed(self) -> None:
        with self.assertRaises(PrivateFormatError):
            parse_external_orders([binance_order_payload(executed_qty="0.001", avg_price="0.0")], symbol=SYMBOL)

    def test_response_must_be_an_array(self) -> None:
        with self.assertRaises(PrivateFormatError):
            parse_external_orders({"symbol": SYMBOL}, symbol=SYMBOL)


class OwnershipBoundaryTest(unittest.TestCase):
    def test_only_probex_prefix_is_ours(self) -> None:
        self.assertTrue(is_probex_order("probex-s1-000001"))
        self.assertFalse(is_probex_order("manual-order-1"))
        self.assertFalse(is_probex_order(""))

    def test_foreign_open_order_is_separated(self) -> None:
        orders = parse_external_orders(
            [binance_order_payload(client_order_id="probex-s1-000001"),
             binance_order_payload(order_id=202, client_order_id="manual-9")],
            symbol=SYMBOL,
        )

        facts = classify_orders(open_orders=orders, history=())

        self.assertEqual([order.client_order_id for order in facts.open_orders], ["probex-s1-000001"])
        self.assertEqual([order.client_order_id for order in facts.foreign_open_orders], ["manual-9"])
        self.assertEqual(facts.history_orders, ())

    def test_foreign_history_is_ignored_not_adopted(self) -> None:
        orders = parse_external_orders(
            [binance_order_payload(order_id=303, client_order_id="bot-x", status="FILLED",
                                   executed_qty="0.001", avg_price="60000.0")],
            symbol=SYMBOL,
        )

        facts = classify_orders(open_orders=(), history=orders)

        self.assertEqual(facts.history_orders, ())
        self.assertEqual(facts.foreign_open_orders, ())
        self.assertEqual(facts.foreign_ignored, 1)

    def test_latest_per_client_order_id_wins(self) -> None:
        orders = parse_external_orders(
            [binance_order_payload(status="NEW", update_time=10),
             binance_order_payload(status="FILLED", executed_qty="0.002", avg_price="60000.0", update_time=15),
             binance_order_payload(status="CANCELED", update_time=20)],
            symbol=SYMBOL,
        )

        latest = latest_by_client_order_id(orders)

        self.assertEqual(len(latest), 1)
        self.assertIs(latest["probex-s1-000001"].status, OrderStatus.CANCELED)


class FillNormalizationTest(unittest.TestCase):
    def test_trade_is_normalized_via_order_id_mapping(self) -> None:
        facts = parse_external_fills(
            [binance_trade_payload()], symbol=SYMBOL, order_id_to_client_id={101: "probex-s1-000001"}
        )

        self.assertEqual(len(facts.fills), 1)
        fill = facts.fills[0]
        self.assertEqual(fill.client_order_id, "probex-s1-000001")
        self.assertEqual(fill.execution_id, "binance-trade-555")
        self.assertEqual(fill.trade_id, "555")
        self.assertAlmostEqual(fill.quantity, 0.001)
        self.assertAlmostEqual(fill.fee, 0.012)
        self.assertEqual(fill.fee_asset, "USDT")
        self.assertEqual(facts.unresolved_order_ids, 0)

    def test_unknown_order_id_is_not_claimed_as_ours(self) -> None:
        facts = parse_external_fills(
            [binance_trade_payload(order_id=999)], symbol=SYMBOL, order_id_to_client_id={101: "probex-s1-000001"}
        )

        self.assertEqual(facts.fills, ())
        self.assertEqual(facts.unresolved_order_ids, 1)

    def test_foreign_order_fill_is_not_claimed_as_ours(self) -> None:
        facts = parse_external_fills(
            [binance_trade_payload()], symbol=SYMBOL, order_id_to_client_id={101: "manual-9"}
        )

        self.assertEqual(facts.fills, ())
        self.assertEqual(facts.unresolved_order_ids, 1)

    def test_non_settlement_commission_fails_closed(self) -> None:
        with self.assertRaises(PrivateFormatError):
            parse_external_fills(
                [binance_trade_payload(commission_asset="BNB")],
                symbol=SYMBOL,
                order_id_to_client_id={101: "probex-s1-000001"},
            )

    def test_missing_fields_fail_closed(self) -> None:
        for field in ("id", "price", "qty", "commission", "time"):
            raw = binance_trade_payload()
            del raw[field]
            with self.subTest(field=field):
                with self.assertRaises(PrivateFormatError):
                    parse_external_fills([raw], symbol=SYMBOL, order_id_to_client_id={101: "probex-s1-000001"})


if __name__ == "__main__":
    unittest.main()
