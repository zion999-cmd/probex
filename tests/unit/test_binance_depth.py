"""SC-1：Binance USDⓈ-M 深度报文 -> 统一 MarketEvent 的边界测试。

报文使用 Binance 真实字段与格式（`depthUpdate` 含 `pu` 等未使用字段，
快照为 REST 响应体）。
"""

from __future__ import annotations

import unittest

from connectors.binance.market_data import MarketDataFormatError, parse_depth_diff, parse_depth_snapshot
from market.events.payloads import BookDeltaPayload, BookSnapshotPayload
from market.events.types import EventType, Venue

RECEIVE_TS = 1_700_000_000_010
PROCESS_TS = 1_700_000_000_020

VALID_DIFF: dict[str, object] = {
    "e": "depthUpdate",
    "E": 1_700_000_000_000,
    "T": 1_700_000_000_005,
    "s": "BTCUSDT",
    "U": 157,
    "u": 160,
    "pu": 156,
    "b": [["0.00240", "10"]],
    "a": [["0.00245", "5"], ["0.00250", "1"]],
}

VALID_SNAPSHOT: dict[str, object] = {
    "lastUpdateId": 156,
    "E": 1_700_000_000_000,
    "T": 1_700_000_000_000,
    "bids": [["0.00240", "10"], ["0.00239", "20"]],
    "asks": [["0.00245", "5"]],
}


def _diff(**overrides: object) -> dict[str, object]:
    raw = dict(VALID_DIFF)
    raw.update(overrides)
    return raw


def _snapshot(**overrides: object) -> dict[str, object]:
    raw = dict(VALID_SNAPSHOT)
    raw.update(overrides)
    return raw


class DepthNormalizationAcceptanceTest(unittest.TestCase):
    """SC-1 验收：合法报文产出 8 字段齐备的 MarketEvent。"""

    def test_sc1_depth_diff_produces_full_market_event(self) -> None:
        event = parse_depth_diff(_diff(), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)

        self.assertIs(event.venue, Venue.BINANCE)
        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertIs(event.event_type, EventType.BOOK_DELTA)
        self.assertEqual(event.exchange_ts, 1_700_000_000_000)
        self.assertEqual(event.receive_ts, RECEIVE_TS)
        self.assertEqual(event.process_ts, PROCESS_TS)
        self.assertEqual(event.sequence, 160)

        payload = event.payload
        self.assertIsInstance(payload, BookDeltaPayload)
        assert isinstance(payload, BookDeltaPayload)
        self.assertEqual((payload.first_update_id, payload.last_update_id), (157, 160))
        self.assertEqual([(lvl.price, lvl.size) for lvl in payload.bids], [(0.0024, 10.0)])
        self.assertEqual([(lvl.price, lvl.size) for lvl in payload.asks], [(0.00245, 5.0), (0.0025, 1.0)])

    def test_sc1_depth_snapshot_produces_full_market_event(self) -> None:
        event = parse_depth_snapshot(
            _snapshot(), symbol="BTCUSDT", receive_ts=RECEIVE_TS, process_ts=PROCESS_TS
        )

        self.assertIs(event.venue, Venue.BINANCE)
        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertIs(event.event_type, EventType.BOOK_SNAPSHOT)
        self.assertEqual(event.exchange_ts, 1_700_000_000_000)
        self.assertEqual(event.receive_ts, RECEIVE_TS)
        self.assertEqual(event.process_ts, PROCESS_TS)
        self.assertEqual(event.sequence, 156)

        payload = event.payload
        self.assertIsInstance(payload, BookSnapshotPayload)
        assert isinstance(payload, BookSnapshotPayload)
        self.assertEqual(payload.last_update_id, 156)
        self.assertEqual([(lvl.price, lvl.size) for lvl in payload.bids], [(0.0024, 10.0), (0.00239, 20.0)])

    def test_sc1_zero_size_delta_level_means_delete(self) -> None:
        event = parse_depth_diff(_diff(b=[["0.00240", "0"]]), receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)
        payload = event.payload
        assert isinstance(payload, BookDeltaPayload)
        self.assertEqual(payload.bids[0].size, 0.0)

    def test_sc1_unknown_extra_fields_are_ignored(self) -> None:
        raw = _diff()
        raw["stream"] = "btcusdt@depth@100ms"
        event = parse_depth_diff(raw, receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)
        self.assertEqual(event.sequence, 160)


class DepthNormalizationBoundaryTest(unittest.TestCase):
    """SC-1 验收：非法报文在边界被拒绝。"""

    def assert_diff_rejected(self, raw: object) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_depth_diff(raw, receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)

    def assert_snapshot_rejected(self, raw: object) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_depth_snapshot(raw, symbol="BTCUSDT", receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)

    def test_raw_must_be_mapping(self) -> None:
        self.assert_diff_rejected([1, 2, 3])
        self.assert_diff_rejected("depthUpdate")
        self.assert_snapshot_rejected(None)

    def test_missing_fields_rejected(self) -> None:
        for field in ("e", "E", "s", "U", "u", "b", "a"):
            with self.subTest(field=field):
                raw = _diff()
                del raw[field]
                self.assert_diff_rejected(raw)

    def test_wrong_event_name_rejected(self) -> None:
        self.assert_diff_rejected(_diff(e="aggTrade"))

    def test_update_ids_must_be_integers(self) -> None:
        for field in ("U", "u", "E"):
            with self.subTest(field=field):
                self.assert_diff_rejected(_diff(**{field: "160"}))

    def test_boolean_update_id_rejected(self) -> None:
        self.assert_diff_rejected(_diff(u=True))

    def test_levels_must_be_arrays(self) -> None:
        self.assert_diff_rejected(_diff(b="0.00240"))
        self.assert_diff_rejected(_diff(b=[5]))
        self.assert_snapshot_rejected(_snapshot(bids={"0.00240": "10"}))

    def test_level_pair_must_have_two_elements(self) -> None:
        self.assert_diff_rejected(_diff(b=[["0.00240", "10", "extra"]]))
        self.assert_diff_rejected(_diff(b=[["0.00240"]]))

    def test_non_numeric_price_rejected(self) -> None:
        self.assert_diff_rejected(_diff(a=[["abc", "1"]]))
        self.assert_diff_rejected(_diff(a=[[None, "1"]]))

    def test_non_positive_price_rejected(self) -> None:
        self.assert_diff_rejected(_diff(a=[["0", "1"]]))
        self.assert_diff_rejected(_diff(a=[["-1", "1"]]))

    def test_negative_size_rejected(self) -> None:
        self.assert_diff_rejected(_diff(a=[["0.00245", "-1"]]))

    def test_non_finite_number_rejected(self) -> None:
        self.assert_diff_rejected(_diff(a=[["nan", "1"]]))
        self.assert_diff_rejected(_diff(a=[["inf", "1"]]))

    def test_inverted_update_range_rejected(self) -> None:
        self.assert_diff_rejected(_diff(U=161, u=160))

    def test_duplicate_price_rejected(self) -> None:
        self.assert_diff_rejected(_diff(a=[["0.00245", "1"], ["0.00245", "2"]]))
        self.assert_snapshot_rejected(_snapshot(bids=[["0.00240", "1"], ["0.00240", "2"]]))

    def test_negative_timestamp_rejected(self) -> None:
        self.assert_diff_rejected(_diff(E=-1))

    def test_snapshot_requires_symbol_argument(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_depth_snapshot(_snapshot(), symbol="", receive_ts=RECEIVE_TS, process_ts=PROCESS_TS)

    def test_snapshot_missing_fields_rejected(self) -> None:
        for field in ("lastUpdateId", "E", "bids", "asks"):
            with self.subTest(field=field):
                raw = _snapshot()
                del raw[field]
                self.assert_snapshot_rejected(raw)

    def test_snapshot_zero_size_level_rejected(self) -> None:
        self.assert_snapshot_rejected(_snapshot(bids=[["0.00240", "0"]]))

    def test_snapshot_last_update_id_must_be_integer(self) -> None:
        self.assert_snapshot_rejected(_snapshot(lastUpdateId="156"))
        self.assert_snapshot_rejected(_snapshot(lastUpdateId=None))


if __name__ == "__main__":
    unittest.main()
