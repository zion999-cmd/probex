"""Bar OHLCV 回测双模式集成（D-056 bar 授权）。

- limit_conservative：post-only 限价在下一根 bar 触碰才成交、maker 费率；
- close_market：非 post-only 单在下一根 open 按固定 bps 滑点成交、taker 费率；
- 输入只能是 BAR 事件（混入 LOB/trade ⇒ INVALID_INPUT）；bar 顺序严格。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from backtest.bar_driver import (
    CLOSE_MARKET,
    LIMIT_CONSERVATIVE,
    BarBacktestConfig,
    run_bar_backtest,
)
from backtest.identity import dataset_identity
from market.events.payloads import BarPayload
from market.events.types import EventType, MarketEvent, Venue
from risk.types import OrderProposal, Side
from storage.events.writer import JsonlEventWriter


def _bar_event(open_ts, close_ts, *, open_, high, low, close, volume=10.0,
                symbol="BTCUSDT") -> MarketEvent:
    return MarketEvent(
        venue=Venue.BINANCE, symbol=symbol, event_type=EventType.BAR,
        exchange_ts=close_ts, receive_ts=close_ts, process_ts=close_ts, sequence=None,
        payload=BarPayload(open_ts=open_ts, close_ts=close_ts,
                             interval_ms=close_ts - open_ts,
                             open=open_, high=high, low=low, close=close, volume=volume))


def _write_bars(tmp: pathlib.Path, bars) -> pathlib.Path:
    path = tmp / "bars.jsonl"
    writer = JsonlEventWriter(path)
    try:
        for event in bars:
            writer.append(event)
    finally:
        writer.close()
    return path


def _config(mode: str, **over) -> BarBacktestConfig:
    base = dict(
        symbol="BTCUSDT", initial_balance=1_000_000.0, maker_fee_rate=0.0002,
        fee_asset="USDT", submit_latency_ms=0, cancel_latency_ms=0,
        max_position_notional=1_000_000.0, max_open_order_exposure=1_000_000.0,
        bar_mode=mode)
    base.update(over)
    return BarBacktestConfig(**base)


class LimitConservativeBarBacktestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-bar-"))

    def test_limit_buy_fills_on_next_bar_touch(self) -> None:
        store = _write_bars(self.tmp, [
            _bar_event(0, 1_000, open_=100, high=101, low=99, close=100),
            _bar_event(1_000, 2_000, open_=100, high=101, low=99, close=100),
        ])

        def decide(bar, snapshot):
            if bar.close_ts == 1_000:
                return (OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=1.0,
                                         price=99.0, post_only=True),)
            return ()

        result = run_bar_backtest(
            store, _config(LIMIT_CONSERVATIVE), run_id="bar-1",
            dataset_sha256=dataset_identity([store])["sha256"], decision_fn=decide)
        fills = result.accounting.fills.fills
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].price, 99.0)
        self.assertAlmostEqual(result.accounting.position("BTCUSDT").qty, 1.0)
        self.assertGreater(result.accounting.trading_fees, 0.0)
        self.assertEqual(result.trace.risk_rejects, 0)

    def test_untouched_limit_remains_open(self) -> None:
        store = _write_bars(self.tmp, [
            _bar_event(0, 1_000, open_=100, high=101, low=99, close=100),
            _bar_event(1_000, 2_000, open_=100, high=100.5, low=99.5, close=100),
        ])

        def decide(bar, snapshot):
            if bar.close_ts == 1_000:
                return (OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=1.0,
                                         price=90.0, post_only=True),)
            return ()

        result = run_bar_backtest(
            store, _config(LIMIT_CONSERVATIVE), run_id="bar-2",
            dataset_sha256=dataset_identity([store])["sha256"], decision_fn=decide)
        self.assertEqual(len(result.accounting.fills.fills), 0)

    def test_mixed_non_bar_events_are_invalid_input(self) -> None:
        from tests.support import trade_event, AggressorSide
        store = self.tmp / "mixed.jsonl"
        writer = JsonlEventWriter(store)
        try:
            writer.append(_bar_event(0, 1_000, open_=100, high=101, low=99, close=100))
            writer.append(trade_event(1, price=100.0, quantity=0.1, aggressor=AggressorSide.BUY,
                                         exchange_ts=1_500, receive_ts=1_500, process_ts=1_500))
        finally:
            writer.close()
        with self.assertRaises(Exception):
            run_bar_backtest(
                store, _config(LIMIT_CONSERVATIVE), run_id="bar-3",
                dataset_sha256="x", decision_fn=lambda bar, snapshot: ())


class CloseMarketBarBacktestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-bar-mkt-"))

    def test_market_buy_fills_at_next_open_with_slippage_and_taker_fee(self) -> None:
        store = _write_bars(self.tmp, [
            _bar_event(0, 1_000, open_=100, high=101, low=99, close=100),
            _bar_event(1_000, 2_000, open_=102, high=103, low=101, close=102.5),
        ])

        def decide(bar, snapshot):
            if bar.close_ts == 1_000:
                return (OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=1.0,
                                         price=100.0, post_only=False),)
            return ()

        cfg = _config(CLOSE_MARKET, taker_slippage_bps=10.0, taker_fee_rate=0.0005)
        result = run_bar_backtest(
            store, cfg, run_id="bar-4",
            dataset_sha256=dataset_identity([store])["sha256"], decision_fn=decide)
        fills = result.accounting.fills.fills
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].price, 102.0 * 1.001)
        self.assertGreater(result.accounting.trading_fees, 102.0 * 0.0004)

    def test_close_market_requires_slippage_and_taker_fee(self) -> None:
        store = _write_bars(self.tmp, [
            _bar_event(0, 1_000, open_=100, high=101, low=99, close=100)])
        cfg = _config(CLOSE_MARKET)  # 未配置滑点/taker 费率
        with self.assertRaises(Exception):
            run_bar_backtest(
                store, cfg, run_id="bar-5",
                dataset_sha256="x", decision_fn=lambda bar, snapshot: ())


    def test_runner_routes_bar_mode_and_reports_granularity(self) -> None:
        import json
        from backtest.report import build_manifest
        from backtest.runner import execute_backtest

        store = _write_bars(self.tmp, [
            _bar_event(0, 1_000, open_=100, high=101, low=99, close=100),
            _bar_event(1_000, 2_000, open_=100, high=101, low=99, close=100),
        ])
        values = {
            "backtest.symbol": "BTCUSDT", "backtest.initial_balance": 1_000_000.0,
            "backtest.maker_fee_rate": 0.0002, "backtest.fee_asset": "USDT",
            "backtest.submit_latency_ms": 0, "backtest.cancel_latency_ms": 0,
            "backtest.bar_mode": LIMIT_CONSERVATIVE,
        }
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps(values), encoding="utf-8")
        out = self.tmp / "out"
        summary = execute_backtest(dataset=store, config_path=config_path,
                                      out_dir=out, prediction=False)
        self.assertEqual(summary["status"], "SUCCESS")
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("bar", manifest["execution_granularity"])


if __name__ == "__main__":
    unittest.main()
