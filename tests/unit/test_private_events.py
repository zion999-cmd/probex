"""P0001.9.2 单元测试：User Data Stream 事件归一化 + 去重/乱序（SC-7 / SC-8 / SC-11）。"""

from __future__ import annotations

import json
import unittest

from connectors.binance.private.errors import PrivateFormatError
from connectors.binance.private.events import (
    AccountUpdateObservation,
    ListenKeyExpiredObservation,
    OrderUpdateObservation,
    UserEventOrdering,
    UserEventType,
    parse_user_event,
)
from tests.private_support import (
    SYMBOL,
    account_update_message,
    listen_key_expired_message,
    order_update_message,
)


class AccountUpdateTest(unittest.TestCase):
    def test_sc7_account_update_is_normalized(self) -> None:
        event = parse_user_event(json.loads(account_update_message()), receive_ts=100, process_ts=200)

        self.assertIs(event.event_type, UserEventType.ACCOUNT_UPDATE)
        self.assertTrue(event.supported)
        observation = event.observation
        self.assertIsInstance(observation, AccountUpdateObservation)
        self.assertEqual(observation.reason, "ORDER")
        self.assertEqual(observation.event_ts, 1_700_000_000_000)
        self.assertEqual((observation.receive_ts, observation.process_ts), (100, 200))
        self.assertEqual(len(observation.balances), 1)
        self.assertAlmostEqual(observation.balances[0].wallet_balance, 1000.50)
        self.assertEqual(len(observation.positions), 1)
        position = observation.positions[0]
        self.assertEqual(position.symbol, SYMBOL)
        self.assertAlmostEqual(position.position_amt, 0.5)
        self.assertAlmostEqual(position.unrealized_profit, 1.5)


class OrderUpdateTest(unittest.TestCase):
    def test_sc8_order_trade_update_is_normalized(self) -> None:
        event = parse_user_event(json.loads(order_update_message()), receive_ts=7, process_ts=8)

        self.assertIs(event.event_type, UserEventType.ORDER_TRADE_UPDATE)
        observation = event.observation
        self.assertIsInstance(observation, OrderUpdateObservation)
        self.assertEqual(observation.symbol, SYMBOL)
        self.assertEqual(observation.client_order_id, "probex-s1-000001")
        self.assertEqual(observation.order_id, 101)
        self.assertEqual(observation.execution_type, "TRADE")
        self.assertEqual(observation.order_status, "FILLED")
        self.assertAlmostEqual(observation.last_fill_quantity, 0.1)
        self.assertAlmostEqual(observation.cumulative_fill_quantity, 0.2)
        self.assertAlmostEqual(observation.last_fill_price, 60000.00)
        self.assertAlmostEqual(observation.average_price, 60000.05)
        self.assertAlmostEqual(observation.commission, 0.12)
        self.assertEqual(observation.commission_asset, "USDT")
        self.assertEqual(observation.trade_id, 77)
        self.assertTrue(observation.is_maker)
        self.assertTrue(observation.is_fill)

    def test_new_order_acknowledgement_is_not_a_fill(self) -> None:
        message = json.loads(
            order_update_message(execution_type="NEW", order_status="NEW", last_fill_quantity="0", cumulative_fill_quantity="0", trade_id=-1)
        )

        observation = parse_user_event(message, receive_ts=1, process_ts=1).observation

        self.assertIsInstance(observation, OrderUpdateObservation)
        self.assertFalse(observation.is_fill)

    def test_missing_order_fields_fail_closed(self) -> None:
        for field in ("c", "i", "S", "o", "x", "X", "l", "z", "L", "ap", "n", "t", "m", "q", "p", "R"):
            message = json.loads(order_update_message())
            del message["o"][field]
            with self.subTest(field=field):
                with self.assertRaises(PrivateFormatError):
                    parse_user_event(message, receive_ts=1, process_ts=1)


class OtherEventsTest(unittest.TestCase):
    def test_listen_key_expired_is_normalized(self) -> None:
        event = parse_user_event(json.loads(listen_key_expired_message()), receive_ts=1, process_ts=2)

        self.assertIs(event.event_type, UserEventType.LISTEN_KEY_EXPIRED)
        self.assertIsInstance(event.observation, ListenKeyExpiredObservation)
        self.assertEqual(event.observation.event_ts, 1_700_000_000_000)

    def test_unsupported_events_are_marked_not_fatal(self) -> None:
        for raw_type in ("MARGIN_CALL", "ACCOUNT_CONFIG_UPDATE", "STRATEGY_UPDATE"):
            event = parse_user_event({"e": raw_type, "E": 1}, receive_ts=1, process_ts=1)
            with self.subTest(raw_type=raw_type):
                self.assertIs(event.event_type, UserEventType.UNSUPPORTED)
                self.assertFalse(event.supported)
                self.assertIsNone(event.observation)

    def test_malformed_events_fail_closed(self) -> None:
        for payload in ({}, {"e": ""}, {"e": "ACCOUNT_UPDATE"}, {"e": "ORDER_TRADE_UPDATE", "o": {}}, 5, "x"):
            with self.subTest(payload=payload):
                with self.assertRaises(PrivateFormatError):
                    parse_user_event(payload, receive_ts=1, process_ts=1)


class OrderingTest(unittest.TestCase):
    def _order_event(self, *, cumulative: str, trade_id: int, client_order_id: str = "cid-1"):
        return parse_user_event(
            json.loads(
                order_update_message(
                    cumulative_fill_quantity=cumulative,
                    last_fill_quantity="0.1",
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                )
            ),
            receive_ts=1,
            process_ts=1,
        )

    def test_sc11_duplicate_order_update_is_not_consumed_twice(self) -> None:
        ordering = UserEventOrdering()
        event = self._order_event(cumulative="0.2", trade_id=77)

        self.assertTrue(ordering.accept(event))
        self.assertFalse(ordering.accept(event))
        self.assertEqual(ordering.duplicate_count, 1)
        self.assertEqual(ordering.out_of_order_count, 0)

    def test_out_of_order_order_update_is_rejected(self) -> None:
        ordering = UserEventOrdering()
        ordering.accept(self._order_event(cumulative="0.2", trade_id=77))

        self.assertFalse(ordering.accept(self._order_event(cumulative="0.1", trade_id=70)))
        self.assertEqual(ordering.out_of_order_count, 1)
        self.assertEqual(ordering.tracked_orders, 1)

    def test_monotonic_progress_is_accepted(self) -> None:
        ordering = UserEventOrdering()

        self.assertTrue(ordering.accept(self._order_event(cumulative="0.1", trade_id=70)))
        self.assertTrue(ordering.accept(self._order_event(cumulative="0.2", trade_id=71)))
        self.assertTrue(ordering.accept(self._order_event(cumulative="0.2", trade_id=72)))  # 同量新 trade id
        self.assertEqual((ordering.duplicate_count, ordering.out_of_order_count), (0, 0))

    def test_orders_are_tracked_independently(self) -> None:
        ordering = UserEventOrdering()

        self.assertTrue(ordering.accept(self._order_event(cumulative="0.5", trade_id=1, client_order_id="cid-1")))
        self.assertTrue(ordering.accept(self._order_event(cumulative="0.1", trade_id=2, client_order_id="cid-2")))
        self.assertEqual(ordering.tracked_orders, 2)

    def test_account_update_watermark_detects_duplicates_and_reordering(self) -> None:
        ordering = UserEventOrdering()
        first = parse_user_event(json.loads(account_update_message(event_ts=100)), receive_ts=1, process_ts=1)
        same = parse_user_event(json.loads(account_update_message(event_ts=100)), receive_ts=2, process_ts=2)
        newer = parse_user_event(json.loads(account_update_message(event_ts=101)), receive_ts=3, process_ts=3)
        older = parse_user_event(json.loads(account_update_message(event_ts=99)), receive_ts=4, process_ts=4)

        self.assertTrue(ordering.accept(first))
        self.assertFalse(ordering.accept(same))
        self.assertTrue(ordering.accept(newer))
        self.assertFalse(ordering.accept(older))
        self.assertEqual(ordering.duplicate_count, 1)
        self.assertEqual(ordering.out_of_order_count, 1)

    def test_unsupported_and_expired_events_are_always_accepted(self) -> None:
        ordering = UserEventOrdering()

        self.assertTrue(ordering.accept(parse_user_event({"e": "MARGIN_CALL", "E": 1}, receive_ts=1, process_ts=1)))
        self.assertTrue(
            ordering.accept(parse_user_event(json.loads(listen_key_expired_message()), receive_ts=1, process_ts=1))
        )
        self.assertEqual((ordering.duplicate_count, ordering.out_of_order_count), (0, 0))


if __name__ == "__main__":
    unittest.main()
