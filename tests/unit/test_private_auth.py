"""P0001.9.2 单元测试：凭据纪律 + 签名 + server-time offset（SC-1 / SC-2 / SC-3）。"""

from __future__ import annotations

import hashlib
import hmac
import unittest

from connectors.binance.private.auth import (
    API_KEY_ENV,
    API_SECRET_ENV,
    ApiCredentials,
    ServerTimeOffset,
    build_signed_request,
    canonical_query,
    sanitize_mapping,
    sanitize_query,
)
from connectors.binance.private.errors import CredentialsError, PrivateAuthError
from tests.private_support import FAKE_API_KEY, FAKE_API_SECRET, credentials

#: RFC 4231 测试向量（HMAC-SHA256, Test Case 1）。
RFC4231_KEY = b"\x0b" * 20
RFC4231_DATA = b"Hi There"
RFC4231_DIGEST = "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"


def _hmac_reference(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class CredentialsTest(unittest.TestCase):
    def test_happy_path_from_env(self) -> None:
        creds = ApiCredentials.from_env({API_KEY_ENV: FAKE_API_KEY, API_SECRET_ENV: FAKE_API_SECRET})

        self.assertEqual(creds.api_key, FAKE_API_KEY)
        self.assertTrue(creds.has_secret)

    def test_missing_credentials_are_rejected(self) -> None:
        for env in ({}, {API_KEY_ENV: FAKE_API_KEY}, {API_SECRET_ENV: FAKE_API_SECRET}):
            with self.subTest(env=sorted(env)):
                with self.assertRaises(CredentialsError) as ctx:
                    ApiCredentials.from_env(env)
                self.assertIn("refuses to start", str(ctx.exception))

    def test_credentials_never_appear_in_repr_or_str(self) -> None:
        creds = credentials()

        for text in (repr(creds), str(creds), f"{creds!r}"):
            self.assertNotIn(FAKE_API_SECRET, text)
            self.assertNotIn(FAKE_API_KEY, text)

    def test_sanitize_helpers_mask_sensitive_keys(self) -> None:
        sanitized = sanitize_mapping({"signature": "abc", "listenKey": "lk", "symbol": "BTCUSDT"})
        query = sanitize_query("symbol=BTCUSDT&signature=deadbeef&listenKey=xyz")

        self.assertEqual(sanitized["signature"], "***")
        self.assertEqual(sanitized["listenKey"], "***")
        self.assertEqual(sanitized["symbol"], "BTCUSDT")
        self.assertNotIn("deadbeef", query)
        self.assertNotIn("xyz", query)


class SigningTest(unittest.TestCase):
    def test_hmac_primitive_matches_rfc4231(self) -> None:
        expected = hmac.new(RFC4231_KEY, RFC4231_DATA, hashlib.sha256).hexdigest()

        self.assertEqual(expected, RFC4231_DIGEST)  # 向量自检

    def test_signature_is_deterministic_hmac_sha256(self) -> None:
        creds = credentials()
        query = "symbol=BTCUSDT&timestamp=1700000000000&recvWindow=5000"

        self.assertEqual(creds.sign(query), _hmac_reference(FAKE_API_SECRET, query))
        self.assertEqual(creds.sign(query), creds.sign(query))

    def test_signed_query_matches_what_was_signed(self) -> None:
        request = build_signed_request(
            path="/fapi/v2/account",
            params={"symbol": "BTCUSDT"},
            credentials=credentials(),
            recv_window_ms=5000,
            timestamp_ms=1_700_000_000_000,
        )
        body, _, signature = request.query.rpartition("&signature=")

        self.assertEqual(signature, credentials().sign(body))
        self.assertIn("timestamp=1700000000000", body)
        self.assertIn("recvWindow=5000", body)

    def test_signed_request_repr_masks_signature(self) -> None:
        request = build_signed_request(
            path="/fapi/v2/account",
            params={},
            credentials=credentials(),
            recv_window_ms=5000,
            timestamp_ms=1_700_000_000_000,
        )

        self.assertNotIn(request.query.split("signature=")[1], repr(request))
        self.assertIn("signature=***", repr(request))

    def test_canonical_query_is_sorted_and_rejects_bad_values(self) -> None:
        self.assertEqual(canonical_query({"b": 2, "a": "x"}), "a=x&b=2")
        self.assertEqual(canonical_query({}), "")
        with self.assertRaises(PrivateAuthError):
            canonical_query({"flag": True})
        with self.assertRaises(PrivateAuthError):
            canonical_query({"none": None})

    def test_invalid_request_inputs_are_rejected(self) -> None:
        with self.assertRaises(PrivateAuthError):
            build_signed_request(path="account", params={}, credentials=credentials(), recv_window_ms=1, timestamp_ms=1)
        with self.assertRaises(PrivateAuthError):
            build_signed_request(
                path="/fapi/v2/account", params={}, credentials=credentials(), recv_window_ms=0, timestamp_ms=1
            )
        with self.assertRaises(PrivateAuthError):
            build_signed_request(
                path="/fapi/v2/account", params={}, credentials=credentials(), recv_window_ms=1, timestamp_ms=0
            )
        with self.assertRaises(PrivateAuthError):
            credentials().sign("")


class ServerTimeOffsetTest(unittest.TestCase):
    def test_offset_measurement_and_timestamps(self) -> None:
        offset = ServerTimeOffset()

        self.assertFalse(offset.measured)
        self.assertEqual(offset.measure(server_time_ms=1_700_000_001_000, local_receive_ms=1_700_000_000_500, round_trip_ms=200), 600)
        self.assertTrue(offset.measured)
        self.assertEqual(offset.timestamp_ms(local_now_ms=1_700_000_010_000), 1_700_000_010_600)

    def test_unmeasured_offset_falls_back_to_local_time(self) -> None:
        offset = ServerTimeOffset()

        self.assertEqual(offset.timestamp_ms(local_now_ms=1_700_000_000_000), 1_700_000_000_000)

    def test_invalid_measurements_are_rejected(self) -> None:
        offset = ServerTimeOffset()

        with self.assertRaises(PrivateAuthError):
            offset.measure(server_time_ms=0, local_receive_ms=1, round_trip_ms=0)
        with self.assertRaises(PrivateAuthError):
            offset.measure(server_time_ms=1, local_receive_ms=1, round_trip_ms=-1)
        with self.assertRaises(PrivateAuthError):
            offset.timestamp_ms(local_now_ms=0)


if __name__ == "__main__":
    unittest.main()
