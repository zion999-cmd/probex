"""SC-1 / SC-6：canonical Jev payload 与字段隔离。"""

from __future__ import annotations

import json
import re
import unittest
from collections.abc import Mapping, Sequence

from prediction.schema.market_v1 import (
    FORBIDDEN_PAYLOAD_FIELDS,
    PAYLOAD_TOP_LEVEL_KEYS,
    QUESTION_SCHEMA_VERSION,
    build_jev_payload,
    build_jev_payload_json,
    market_state_hash,
)
from prediction.types import FUTURE_RETURN_CATEGORIES
from tests.support import warm_market_states

HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _keys(value: object) -> list[str]:
    """递归收集所有 JSON 键名。"""
    if isinstance(value, Mapping):
        found: list[str] = []
        for key, item in value.items():
            found.append(str(key))
            found.extend(_keys(item))
        return found
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        found = []
        for item in value:
            found.extend(_keys(item))
        return found
    return []


class JevPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = warm_market_states(1)[0]
        self.payload = build_jev_payload(self.state)

    def test_sc1_same_state_produces_identical_payload_and_hash(self) -> None:
        first = build_jev_payload_json(self.state)
        second = build_jev_payload_json(self.state)

        self.assertEqual(first, second)
        self.assertEqual(market_state_hash(self.state), market_state_hash(self.state))
        self.assertEqual(self.payload["market_state_hash"], market_state_hash(self.state))

    def test_hash_has_expected_format(self) -> None:
        self.assertRegex(market_state_hash(self.state), HASH_PATTERN)

    def test_different_state_produces_different_hash(self) -> None:
        other = warm_market_states(2)[1]
        self.assertNotEqual(market_state_hash(self.state), market_state_hash(other))
        self.assertNotEqual(build_jev_payload_json(self.state), build_jev_payload_json(other))

    def test_versions_and_identity_are_present(self) -> None:
        self.assertEqual(self.payload["question_schema_version"], QUESTION_SCHEMA_VERSION)
        self.assertEqual(self.payload["feature_schema_version"], self.state.feature_schema_version)
        identity = self.payload["identity"]
        assert isinstance(identity, dict)
        self.assertEqual(identity["venue"], self.state.identity.venue.value)
        self.assertEqual(identity["symbol"], self.state.identity.symbol)

    def test_as_of_mirrors_state_time(self) -> None:
        as_of = self.payload["as_of"]
        assert isinstance(as_of, dict)
        self.assertEqual(as_of["as_of_exchange_ts"], self.state.time.as_of_exchange_ts)
        self.assertEqual(as_of["as_of_receive_ts"], self.state.time.as_of_receive_ts)
        self.assertEqual(as_of["event_ordinal"], self.state.time.event_ordinal)

    def test_market_sections_mirror_state_features(self) -> None:
        price = self.payload["price"]
        depth = self.payload["depth"]
        quality = self.payload["quality"]
        assert isinstance(price, dict) and isinstance(depth, dict) and isinstance(quality, dict)

        self.assertEqual(price["mid"], self.state.price.mid)
        self.assertEqual(price["microprice"], self.state.price.microprice)
        self.assertEqual(depth["bid_depth_5"], self.state.depth.bid_depth_5)
        self.assertEqual(depth["vamp"], self.state.depth.vamp)
        self.assertEqual(quality["book_health"], self.state.quality.book_health.value)
        self.assertEqual(quality["tradeable"], self.state.quality.tradeable)

    def test_questions_are_embedded_not_implicit(self) -> None:
        questions = self.payload["questions"]
        assert isinstance(questions, list)
        by_key = {spec["key"]: spec for spec in questions}  # type: ignore[index]

        self.assertEqual(set(by_key), {"future_return", "buy_adverse_selection", "sell_adverse_selection", "buy_fill_probability", "sell_fill_probability"})
        self.assertEqual(by_key["future_return"]["horizons"], ["5s", "15s", "30s", "60s"])  # type: ignore[index]
        self.assertEqual(by_key["future_return"]["categories"], list(FUTURE_RETURN_CATEGORIES))  # type: ignore[index]
        self.assertEqual(by_key["future_return"]["required_probability_sum"], 1.0)  # type: ignore[index]

    def test_trade_section_is_available_but_unavailable_in_data(self) -> None:
        trade = self.payload["trade"]
        assert isinstance(trade, dict)
        self.assertFalse(trade["trade_stream_available"])
        self.assertIsNone(trade["cvd"])

    def test_payload_json_is_canonical(self) -> None:
        text = build_jev_payload_json(self.state)
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        self.assertNotIn(": ", text)


class PayloadFieldIsolationTest(unittest.TestCase):
    """SC-6 验收：Jev 看不到账户 / 仓位 / 盈亏 / 风险等未来领域对象。"""

    def setUp(self) -> None:
        self.payload = build_jev_payload(warm_market_states(1)[0])

    def test_top_level_keys_are_exactly_the_allowlist(self) -> None:
        self.assertEqual(sorted(self.payload), sorted(PAYLOAD_TOP_LEVEL_KEYS))

    def test_no_forbidden_field_name_appears_anywhere(self) -> None:
        leaked = sorted({key for key in _keys(self.payload) if key in FORBIDDEN_PAYLOAD_FIELDS})
        self.assertEqual(leaked, [])

    def test_no_action_or_decision_fields(self) -> None:
        keys = {key.lower() for key in _keys(self.payload)}
        for forbidden in ("buy", "sell", "hold", "action", "order", "orders", "signal", "target_position"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, keys)

    def test_serialized_payload_contains_no_account_terms(self) -> None:
        # 按键名分词检查：避免 "depth_imbalance_1" 这类字段误报
        payload = build_jev_payload(warm_market_states(1)[0])
        tokens = {token for key in _keys(payload) for token in key.lower().split("_")}
        for term in ("balance", "position", "positions", "pnl", "equity", "leverage", "liquidation", "account", "margin", "risk"):
            with self.subTest(term=term):
                self.assertNotIn(term, tokens)


if __name__ == "__main__":
    unittest.main()
