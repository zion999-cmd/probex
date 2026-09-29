"""P0001.10 产品序列化单测（schema 版本化 + UNKNOWN 显式 + 拒绝 NaN）。"""

from __future__ import annotations

import json
import unittest

from product.serialization import SerializationError, snapshot_to_json, snapshot_to_jsonable, to_jsonable
from product.types import SCHEMA_VERSION, Fact, RuntimeIdentity, RuntimeMode
from tests.unit.test_product_snapshot import service

REQUIRED_SECTIONS = ("runtime", "market", "prediction", "strategy", "risk", "execution", "portfolio",
                     "readiness", "health", "evidence")


class SerializationContractTest(unittest.TestCase):
    def test_snapshot_payload_is_versioned_and_complete(self) -> None:
        payload = snapshot_to_jsonable(service().snapshot())
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        for section in REQUIRED_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(section, payload)

    def test_fact_encodes_known_and_reason(self) -> None:
        self.assertEqual(to_jsonable(Fact.of(3)), {"known": True, "value": 3, "reason": None})
        self.assertEqual(to_jsonable(Fact.unknown("not_available_yet")),
                         {"known": False, "value": None, "reason": "not_available_yet"})

    def test_unknown_survives_round_trip_as_unknown(self) -> None:
        payload = snapshot_to_jsonable(service(market_state=lambda: None).snapshot())
        self.assertFalse(payload["market"]["best_bid"]["known"])
        self.assertIsNone(payload["market"]["best_bid"]["value"])
        self.assertTrue(payload["market"]["best_bid"]["reason"])

    def test_json_is_strict_and_parsable(self) -> None:
        text = snapshot_to_json(service().snapshot())
        self.assertEqual(json.loads(text)["schema_version"], SCHEMA_VERSION)

    def test_nan_and_infinity_are_refused(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(SerializationError):
                    to_jsonable(bad)

    def test_unknown_types_are_refused_not_guessed(self) -> None:
        class Opaque:
            pass

        with self.assertRaises(SerializationError):
            to_jsonable(Opaque())

    def test_enums_serialize_to_their_value(self) -> None:
        payload = to_jsonable(RuntimeIdentity(mode=RuntimeMode.TESTNET, environment="testnet",
                                              venue="binance", symbol="BTCUSDT", runtime_id="rt",
                                              started_at=1, data_timestamp=Fact.of(2)))
        self.assertEqual(payload["mode"], "TESTNET")
