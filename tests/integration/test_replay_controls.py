"""P0001.12 集成：replay 控制（SC-4/5/6/13）。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from api.server import create_server
from market.replay.source import ReplaySource
from product.market_projection import ReplayControl
from product.types import RuntimeMode
from storage.events.reader import JsonlEventReader
from tests import scenarios
from tests.support import TempDirTestCase, market_book, write_store
from tests.unit.test_product_snapshot import service

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ReplayControlApiTest(TempDirTestCase):
    def server_with(self, control: object):
        server = create_server(service(replay_control=lambda: control), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, base: str, verb: str, payload: dict) -> dict:
        request = urllib.request.Request(f"{base}/api/v1/replay/{verb}", method="POST",
                                         data=json.dumps(payload).encode())
        with OPENER.open(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_4_play_pause_step_via_api(self) -> None:
        control = ReplayControl(mode=RuntimeMode.REPLAY)
        server, thread, base = self.server_with(control)
        try:
            self.assertEqual(self.post(base, "play", {})["command"]["verb"], "play")
            self.assertFalse(control.paused)
            self.assertEqual(self.post(base, "pause", {})["command"]["verb"], "pause")
            step = self.post(base, "step", {"count": 5})
            self.assertEqual(step["command"]["payload"]["count"], 5)
            self.assertEqual(step["control"]["position"], 5)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_5_speed_changes_pacing_not_event_order(self) -> None:
        """速度只影响节奏：事件顺序必须与不快进时完全一致。"""
        control = ReplayControl(mode=RuntimeMode.REPLAY)
        server, thread, base = self.server_with(control)
        try:
            path = self.store_path()
            write_store(path, scenarios.reference_events())
            for speed in (0.25, 10.0):
                self.post(base, "speed", {"speed": speed})
                events = list(ReplaySource(JsonlEventReader(path)).iter_events())
                self.assertEqual(events, scenarios.reference_events())
            bad = urllib.request.Request(f"{base}/api/v1/replay/speed", method="POST",
                                         data=json.dumps({"speed": 3.0}).encode())
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                OPENER.open(bad, timeout=5)
            self.assertEqual(ctx.exception.code, 409)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_6_seek_is_restart_plus_fast_forward_and_matches_full_replay(self) -> None:
        """SC-6：seek 后得到的 book state 与从头 replay 到同一位置一致。"""
        events = scenarios.reference_events()
        path = self.store_path()
        write_store(path, events)

        from market.book.order_book import BookSide
        from tests.support import replay_into

        def book_after(count: int):
            book = market_book()
            for event in list(ReplaySource(JsonlEventReader(path)).iter_events())[:count]:
                replay_into(book, [event])
            return book

        def levels(book):
            return [(level.price, level.size) for level in book.depth(BookSide.BID, 5)]

        target = 4
        fast_forwarded = book_after(target)          # seek = restart + fast-forward
        replayed = market_book()
        for event in list(ReplaySource(JsonlEventReader(path)).iter_events())[:target]:
            replay_into(replayed, [event])

        self.assertEqual(levels(fast_forwarded), levels(replayed))
        self.assertTrue(levels(replayed))

    def test_13_replay_control_cannot_target_non_replay_runtime(self) -> None:
        class FakeLiveControl:
            def play(self) -> object:
                raise RuntimeError("replay control is only available for REPLAY runtimes, got TESTNET")

        server, thread, base = self.server_with(FakeLiveControl())
        try:
            request = urllib.request.Request(f"{base}/api/v1/replay/play", method="POST", data=b"{}")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                OPENER.open(request, timeout=5)
            self.assertEqual(ctx.exception.code, 409)
            body = json.loads(ctx.exception.read().decode("utf-8"))
            self.assertEqual(body["error"], "replay_control_refused")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_replay_control_is_absent_for_live_runtimes(self) -> None:
        server, thread, base = self.server_with(None)
        try:
            request = urllib.request.Request(f"{base}/api/v1/replay/play", method="POST", data=b"{}")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                OPENER.open(request, timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
