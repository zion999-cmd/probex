"""P0001.12 投影单测（SC-1/2/3/8/9/10/11）+ 只读与边界（SC-14）。"""

from __future__ import annotations

import pathlib
import unittest
from types import SimpleNamespace

from product.market_projection import (
    ALLOWED_SPEEDS,
    BoundedMarketHistory,
    MarketProjectionConfig,
    ProjectionError,
    ReplayControl,
    ReplayControlError,
    project_depth,
    project_health,
    project_overlays,
    project_trades,
)
from product.market_timeline import point_from_state, project_timeline
from product.types import Fact, RuntimeMode

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


def config(**overrides: object) -> MarketProjectionConfig:
    values: dict[str, object] = {"window_ms": 600_000, "bucket_ms": 1_000, "max_points": 10,
                                 "price_levels": 5}
    values.update(overrides)
    return MarketProjectionConfig(**values)  # type: ignore[arg-type]


def state(ts: int, *, bid: float = 100.0, ask: float = 101.0, microprice: float | None = 100.4,
          imbalance: float | None = 0.1, ofi: float | None = 2.0, health: str = "healthy",
          completeness: float | None = 0.9) -> object:
    return SimpleNamespace(
        time=SimpleNamespace(as_of_exchange_ts=ts, event_ordinal=0),
        quality=SimpleNamespace(book_health=health, completeness=completeness),
        price=SimpleNamespace(best_bid=bid, best_ask=ask, mid=(bid + ask) / 2, spread=ask - bid,
                              microprice=microprice),
        depth=SimpleNamespace(l1_imbalance=imbalance),
        flow=SimpleNamespace(ofi_1s=ofi),
    )


def snapshot(ts: int, *, bid: float = 100.0, ask: float = 101.0) -> object:
    return SimpleNamespace(ts=ts, bids=[(bid, 1.5), (bid - 1, 2.0)], asks=[(ask, 1.0), (ask + 1, 0.5)])


class TimelineProjectionTest(unittest.TestCase):
    def test_timeline_mirrors_owner_feature_values(self) -> None:
        """SC-7：特征来自既有 Owner，投影不重算。"""
        timeline = project_timeline([state(1_000, microprice=100.4, imbalance=0.25, ofi=3.0)],
                                    bucket_ms=1_000, max_points=5)
        point = timeline.points[0]

        self.assertEqual(point.microprice.value, 100.4)
        self.assertEqual(point.imbalance.value, 0.25)
        self.assertEqual(point.ofi.value, 3.0)

    def test_unknown_features_stay_unknown(self) -> None:
        """SC-11：UNKNOWN 不能被画成 0。"""
        point = project_timeline([state(1_000, microprice=None, imbalance=None, ofi=None)],
                                 bucket_ms=1_000, max_points=5).points[0]

        for fact in (point.microprice, point.imbalance, point.ofi):
            with self.subTest(reason=fact.reason):
                self.assertFalse(fact.known)
                self.assertIsNone(fact.value)

    def test_point_requires_a_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            point_from_state(SimpleNamespace(price=None, depth=None, flow=None, quality=None, time=None))


class DepthProjectionTest(unittest.TestCase):
    def test_depth_buckets_carry_best_bid_ask_and_mid(self) -> None:
        """SC-1/SC-2：热图单元存在，且 best/mid 与盘口快照一致。"""
        heatmap = project_depth([snapshot(1_000), snapshot(2_000, bid=100.5, ask=101.5)], config=config())

        self.assertTrue(heatmap.cells)
        self.assertEqual(heatmap.bid_series[-1], (2_000, 100.5))
        self.assertEqual(heatmap.ask_series[-1], (2_000, 101.5))
        self.assertAlmostEqual(heatmap.mid_series[-1][1], 101.0)

    def test_depth_respects_price_levels_and_window(self) -> None:
        heatmap = project_depth([snapshot(ts) for ts in range(0, 10_000, 1_000)],
                                config=config(price_levels=1, max_points=3, window_ms=3_000))

        self.assertEqual(heatmap.price_levels, 1)
        self.assertLessEqual(len({cell.bucket_ts for cell in heatmap.cells}), 3)
        self.assertTrue(all(cell.quantity in (1.5, 1.0) for cell in heatmap.cells))

    def test_depth_never_produces_fills(self) -> None:
        """SC-3：盘口消失不得被解释成成交。"""
        heatmap = project_depth([snapshot(1_000), SimpleNamespace(ts=2_000, bids=[], asks=[])],
                                config=config())

        self.assertIn("NOT fill evidence", heatmap.notes[0])
        self.assertFalse(hasattr(heatmap, "fills"))

    def test_empty_projection_is_explicitly_unknown_not_empty_plot(self) -> None:
        heatmap = project_depth([], config=config())
        self.assertEqual(heatmap.cells, ())
        self.assertTrue(any("no book snapshots" in note for note in heatmap.notes))


class TradeAndHealthProjectionTest(unittest.TestCase):
    def test_trades_come_only_from_trade_payloads(self) -> None:
        """SC-3：trades 必须来自 TradePayload（此处模拟其事实形态）。"""
        trades = [SimpleNamespace(ts=1_000, price=100.0, quantity=0.5, aggressor=SimpleNamespace(value="buy")),
                  SimpleNamespace(ts=2_000, price=101.0, quantity=0.2, aggressor="sell")]
        projection = project_trades(trades, max_points=10)

        self.assertEqual([print.aggressor for print in projection.prints], ["buy", "sell"])
        self.assertIn("never inferred from book deltas", projection.notes[0])

    def test_health_segments_show_transitions(self) -> None:
        """SC-8。"""
        health = project_health([SimpleNamespace(ts=1_000, health=SimpleNamespace(value="healthy"), reason=""),
                                 SimpleNamespace(ts=2_000, health="stale", reason="gap detected",
                                                 market_generation=3)], max_segments=5)

        self.assertEqual([segment.health for segment in health.segments], ["healthy", "stale"])
        self.assertEqual(health.segments[1].reason, "gap detected")
        self.assertEqual(health.segments[1].market_generation, 3)

    def test_overlays_include_decisions_and_execution_events(self) -> None:
        """SC-9/SC-10。"""
        overlays = project_overlays(
            [SimpleNamespace(ts=1_000, side="buy", action=SimpleNamespace(value="place"), price=100.0,
                             quantity=0.001, decision_id="maker:1000:bid", reason="place a passive quote")],
            [SimpleNamespace(ts=1_100, client_order_id="probex-s1-1", event="OrderAccepted", detail="")],
            max_points=10)

        self.assertEqual(overlays.decisions[0].decision_id.value, "maker:1000:bid")
        self.assertEqual(overlays.decisions[0].action, "place")
        self.assertEqual(overlays.executions[0].client_order_id, "probex-s1-1")

    def test_missing_overlay_facts_stay_unknown(self) -> None:
        overlays = project_overlays([SimpleNamespace(ts=1, side="buy", action="keep", price=None,
                                                     quantity=None)], max_points=5)
        self.assertFalse(overlays.decisions[0].price.known)
        self.assertFalse(overlays.decisions[0].decision_id.known)


class BoundedHistoryTest(unittest.TestCase):
    def test_history_is_bounded(self) -> None:
        history = BoundedMarketHistory(capacity=3, run_id="run-1")
        for index in range(10):
            history.feed_state(state(index))
            history.feed_trade(SimpleNamespace(ts=index, price=1.0, quantity=1.0, aggressor="buy"))

        self.assertEqual(len(history.states()), 3)
        self.assertEqual(history.counts["capacity"], 3)
        self.assertEqual(history.counts["snapshots"], 0)
        self.assertEqual(history.run_id, "run-1")

    def test_invalid_capacity_is_refused(self) -> None:
        with self.assertRaises(ProjectionError):
            BoundedMarketHistory(capacity=0)


class ReplayControlTest(unittest.TestCase):
    def test_13_replay_control_refuses_non_replay_runtimes(self) -> None:
        for mode in (RuntimeMode.PAPER, RuntimeMode.TESTNET, RuntimeMode.LIVE):
            with self.subTest(mode=mode.value):
                with self.assertRaises(ReplayControlError):
                    ReplayControl(mode=mode)

    def test_4_play_pause_step_emit_commands(self) -> None:
        control = ReplayControl(mode=RuntimeMode.REPLAY)
        self.assertTrue(control.paused)
        self.assertEqual(control.play().verb, "play")
        self.assertFalse(control.paused)
        self.assertEqual(control.pause().verb, "pause")
        step = control.step(count=3)
        self.assertEqual(step.payload["count"], 3)
        self.assertEqual(control.position, 3)
        self.assertEqual(len(control.commands()), 3)

    def test_speed_must_be_one_of_the_allowed_values(self) -> None:
        control = ReplayControl(mode=RuntimeMode.REPLAY)
        for speed in ALLOWED_SPEEDS:
            self.assertEqual(control.set_speed(speed).payload["speed"], speed)
        with self.assertRaises(ProjectionError):
            control.set_speed(3.0)

    def test_seek_requires_exactly_one_target(self) -> None:
        control = ReplayControl(mode=RuntimeMode.REPLAY)
        self.assertEqual(control.seek(ordinal=42).payload["ordinal"], 42)
        self.assertEqual(control.seek(ts=1_700_000_000_000).payload["ts"], 1_700_000_000_000)
        with self.assertRaises(ProjectionError):
            control.seek()

    def test_14_projection_layer_has_no_market_or_trading_truth(self) -> None:
        """SC-14：投影层不得触碰 MarketBook / engine / 交易组件。"""
        for name in ("market_timeline.py", "market_projection.py"):
            source = (PROJECT_ROOT / "product" / name).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("from ") or stripped.startswith("import "):
                    for root in ("market.book", "market.features", "execution", "strategy", "risk", "connectors", "live"):
                        with self.subTest(module=name, import_line=stripped):
                            self.assertFalse(stripped.startswith(f"from {root}.")
                                             or stripped.startswith(f"import {root}"))
            for forbidden in ("apply(", "submit(", "cancel(", "fill("):
                self.assertNotIn(forbidden, source)
