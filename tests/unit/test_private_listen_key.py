"""P0001.9.2 单元测试：listenKey 生命周期状态机（SC-9）。"""

from __future__ import annotations

import unittest

from connectors.binance.private.errors import ListenKeyError
from connectors.binance.private.user_stream import (
    ALLOWED_TRANSITIONS,
    DOCUMENTED_LISTEN_KEY_TTL_MS,
    ListenKeyLifecycle,
    ListenKeyState,
    UserStreamClient,
    transition_allowed,
)

TTL = 60_000


def _active(ttl: int = TTL) -> ListenKeyLifecycle:
    lifecycle = ListenKeyLifecycle(ttl_ms=ttl)
    lifecycle.start_creating(now_ms=0)
    lifecycle.on_created("LK-SECRET-VALUE", now_ms=0)
    return lifecycle


class LifecycleTest(unittest.TestCase):
    def test_documented_ttl_constant_matches_official_guidance(self) -> None:
        self.assertEqual(DOCUMENTED_LISTEN_KEY_TTL_MS, 60 * 60 * 1000)

    def test_happy_path_transitions(self) -> None:
        lifecycle = ListenKeyLifecycle(ttl_ms=TTL)

        self.assertIs(lifecycle.state, ListenKeyState.STOPPED)
        lifecycle.start_creating(now_ms=0)
        self.assertIs(lifecycle.state, ListenKeyState.STARTING)
        lifecycle.on_created("LK-1", now_ms=10)
        self.assertIs(lifecycle.state, ListenKeyState.ACTIVE)
        self.assertEqual((lifecycle.create_count, lifecycle.expires_at_ms), (1, 10 + TTL))
        lifecycle.mark_renewing()
        lifecycle.on_renewed(now_ms=20)
        self.assertIs(lifecycle.state, ListenKeyState.ACTIVE)
        self.assertEqual((lifecycle.renew_count, lifecycle.expires_at_ms), (1, 20 + TTL))
        lifecycle.mark_reconnecting()
        lifecycle.on_reconnected()
        self.assertIs(lifecycle.state, ListenKeyState.ACTIVE)

    def test_expiry_then_recreate(self) -> None:
        lifecycle = _active()

        lifecycle.on_expired(now_ms=TTL, reason="listenKeyExpired")
        self.assertIs(lifecycle.state, ListenKeyState.EXPIRED)
        self.assertFalse(lifecycle.has_key)
        self.assertEqual(lifecycle.expired_count, 1)

        lifecycle.start_creating(now_ms=TTL)
        lifecycle.on_created("LK-2", now_ms=TTL)

        self.assertIs(lifecycle.state, ListenKeyState.ACTIVE)
        self.assertEqual(lifecycle.create_count, 2)

    def test_ttl_expiry_detection_and_keepalive_scheduling(self) -> None:
        lifecycle = _active()

        self.assertFalse(lifecycle.is_expired(now_ms=TTL - 1))
        self.assertTrue(lifecycle.is_expired(now_ms=TTL))
        self.assertTrue(lifecycle.needs_keepalive(now_ms=1000, interval_ms=1000))
        self.assertFalse(lifecycle.needs_keepalive(now_ms=999, interval_ms=1000))
        lifecycle.mark_renewing()
        self.assertFalse(lifecycle.needs_keepalive(now_ms=9999, interval_ms=1))  # RENEWING 不重复触发

    def test_stopped_lifecycle_reports_no_expiry_or_keepalive(self) -> None:
        lifecycle = ListenKeyLifecycle(ttl_ms=TTL)

        self.assertFalse(lifecycle.is_expired(now_ms=10**9))
        self.assertFalse(lifecycle.needs_keepalive(now_ms=10**9, interval_ms=1))

    def test_illegal_transitions_are_rejected(self) -> None:
        lifecycle = ListenKeyLifecycle(ttl_ms=TTL)

        with self.assertRaises(ListenKeyError):
            lifecycle.on_renewed(now_ms=1)
        with self.assertRaises(ListenKeyError):
            lifecycle.stop()
            lifecycle.on_created("LK-x", now_ms=1)

    def test_failure_path(self) -> None:
        lifecycle = _active()

        lifecycle.fail("keepalive failed")

        self.assertIs(lifecycle.state, ListenKeyState.FAILED)
        self.assertEqual(lifecycle.failure_reason, "keepalive failed")
        lifecycle.stop()
        self.assertIs(lifecycle.state, ListenKeyState.STOPPED)
        with self.assertRaises(ListenKeyError):
            lifecycle.fail("again")

    def test_sanitized_view_never_contains_the_key(self) -> None:
        lifecycle = _active()

        view = lifecycle.sanitized_view()
        rendered = f"{view} {lifecycle!r} {lifecycle}"

        self.assertTrue(view["has_key"])
        self.assertNotIn("LK-SECRET-VALUE", rendered)
        self.assertNotIn("LK-", rendered)

    def test_invalid_construction_and_inputs(self) -> None:
        with self.assertRaises(ListenKeyError):
            ListenKeyLifecycle(ttl_ms=0)
        lifecycle = ListenKeyLifecycle(ttl_ms=TTL)
        lifecycle.start_creating(now_ms=0)
        with self.assertRaises(ListenKeyError):
            lifecycle.on_created("", now_ms=1)
        with self.assertRaises(ListenKeyError):
            _active().needs_keepalive(now_ms=1, interval_ms=0)

    def test_transition_table_is_explicit(self) -> None:
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(ListenKeyState))
        self.assertTrue(transition_allowed(ListenKeyState.ACTIVE, ListenKeyState.EXPIRED))
        self.assertFalse(transition_allowed(ListenKeyState.STOPPED, ListenKeyState.ACTIVE))
        self.assertFalse(transition_allowed(ListenKeyState.FAILED, ListenKeyState.ACTIVE))


class StreamClientTest(unittest.TestCase):
    def test_user_data_url_carries_the_private_tier_and_listen_key(self) -> None:
        client = UserStreamClient(transport_factory=lambda url, timeout_s: None, ws_host="wss://example.invalid")

        url = client.url("LK-123")
        sanitized = client.sanitized_url("LK-123")

        self.assertEqual(url, "wss://example.invalid/private/ws?listenKey=LK-123")
        self.assertNotIn("LK-123", sanitized)
        self.assertIn("***", sanitized)

    def test_connect_uses_the_injected_factory(self) -> None:
        calls: list[tuple[str, float]] = []

        def factory(url: str, *, timeout_s: float) -> object:
            calls.append((url, timeout_s))
            return object()

        client = UserStreamClient(transport_factory=factory, ws_host="wss://example.invalid")
        client.connect("LK-456", timeout_s=3.0)

        self.assertEqual(calls, [("wss://example.invalid/private/ws?listenKey=LK-456", 3.0)])


if __name__ == "__main__":
    unittest.main()
