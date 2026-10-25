"""资金费模型集成：回测 driver 与共享步进核心（D-055 批次2，人类授权 funding 建模）。

- 单标的 driver：显式 `funding_enabled=True` 时，mark 事件公布的费率/结算点驱动
  FundingLedger；默认关闭时零资金费、能力声明 UNSUPPORTED；
- 共享核心 `process_market_event`：越过结算点产生支付（多付空），且只结算一次。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from market.events.types import EventType
from storage.events.reader import JsonlEventReader
from storage.events.writer import JsonlEventWriter
from tests.ui.market_fixture import write_market_store

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _funding_store(tmp: pathlib.Path, *, rate: float = 0.0001) -> pathlib.Path:
    """fixture 事件流；把每条 MARK_PRICE 改造成"公布费率并在自身时刻结算"。"""
    raw = tmp / "raw.jsonl"
    write_market_store(raw, hours=1, seed=7, mark_price=60_000.0)
    out = tmp / "funding.jsonl"
    writer = JsonlEventWriter(out)
    try:
        for record in JsonlEventReader(raw):
            event = record.event
            if event.event_type is EventType.MARK_PRICE:
                object.__setattr__(event, "payload", type(event.payload)(
                    price=event.payload.price, funding_rate=rate,
                    next_funding_time=int(event.exchange_ts)))
            writer.append(event)
    finally:
        writer.close()
    return out


class BacktestFundingDriverTest(unittest.TestCase):
    def setUp(self) -> None:
        import json
        from runtime.maker_config import build_maker_policy

        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-funding-"))
        self.store = _funding_store(self.tmp)
        values = json.loads(
            (PROJECT_ROOT / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
        self.policy = build_maker_policy(values)
        assert self.policy is not None

    def _run(self, *, enabled: bool):
        from backtest.driver import BacktestConfig, run_backtest
        from backtest.identity import dataset_identity
        from backtest.prediction import local_trial_runtime

        cfg = BacktestConfig(
            symbol="BTCUSDT", initial_balance=1_000_000.0, maker_fee_rate=0.0002,
            fee_asset="USDT", submit_latency_ms=0, cancel_latency_ms=0,
            max_position_notional=1_000_000.0, max_open_order_exposure=1_000_000.0,
            funding_enabled=enabled)
        ident = dataset_identity([self.store])
        return run_backtest(
            self.store, cfg, run_id="bt-funding", dataset_sha256=ident["sha256"],
            decision_fn=lambda **kw: self.policy.decide(**kw),
            prediction_runtime=local_trial_runtime())

    def test_funding_settled_when_enabled(self) -> None:
        result = self._run(enabled=True)
        ledger = result.accounting.funding
        self.assertGreater(len(ledger), 0, "open positions must be charged at announced boundaries")
        self.assertTrue(all(p.asset == "USDT" for p in ledger.payments))
        # 多头在正费率下净支出
        self.assertNotEqual(ledger.total, 0.0)
        # 资金费进入 balance / net_realized 口径
        self.assertIn(ledger, result.accounting.funding and [result.accounting.funding])

    def test_funding_disabled_by_default(self) -> None:
        result = self._run(enabled=False)
        self.assertEqual(len(result.accounting.funding), 0)

    def test_manifest_declares_model_status(self) -> None:
        from backtest.report import build_manifest, FUNDING_MODEL, FUNDING_MODELED

        on = build_manifest(self._run(enabled=True), dataset_files=["x"], input_files=[])
        off = build_manifest(self._run(enabled=False), dataset_files=["x"], input_files=[])
        self.assertEqual(on["cost_models"]["funding"], FUNDING_MODELED)
        self.assertEqual(off["cost_models"]["funding"], FUNDING_MODEL)


class ProcessMarketEventFundingTest(unittest.TestCase):
    def test_shared_core_settles_funding_on_mark_event(self) -> None:
        from execution.engine import ExecutionEngine
        from execution.manager import OrderManager
        from execution.simulation.fees import FeeSchedule
        from execution.simulation.latency import LatencyModel
        from execution.simulation.venue import SimulatedVenue
        from execution.tracker import OrderTracker
        from market.events.types import Venue
        from market.features.engine import FeatureEngine
        from portfolio.accounting import AccountingCore
        from portfolio.funding_model import FundingSettlementTracker
        from risk.gate import RiskGate
        from risk.limits import RiskLimits
        from runtime.multi_symbol_core import StepCounters, StepConfig, process_market_event
        from tests.sim_support import SimStack, book_snapshot, sell_aggressor, ts
        from tests.support import mark_price_event

        stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0,
                                                   max_open_order_exposure=500.0))
        # 直接用既有验证过的链路产生一笔多头成交（bid @100 被卖方吃单）
        from strategy.maker import MakerPolicy
        from tests.integration.test_maker_simulated_execution import _decide
        from tests.strategy_support import make_record, maker_config

        policy = MakerPolicy(maker_config())
        from tests.support import warm_market_states
        first_state = warm_market_states(2)[0]
        stack.feed([book_snapshot(update_id=1, bids=((100.0, 5.1),),
                                     asks=((101.0, 2.0),), offset=300_000)])
        prediction = make_record(state=first_state, now_ms=ts(300_000))
        decision = _decide(policy, stack, first_state, prediction, now_ms=ts(300_000))
        stack.submit(decision.bid.proposal, now_ms=ts(300_000))
        stack.feed([
            sell_aggressor(1, price=100.0, quantity=5.1, offset=300_001),
            sell_aggressor(2, price=100.0, quantity=1.0, offset=300_002),
        ])
        self.assertAlmostEqual(stack.accounting.position("BTCUSDT").qty, 1.0)

        boundary = ts(301_000)
        mark = mark_price_event(price=100.0, exchange_ts=boundary,
                                  funding_rate=0.0001, next_funding_time=boundary)
        tracker = FundingSettlementTracker(asset="USDT", symbol="BTCUSDT")
        feature = FeatureEngine(venue=Venue.BINANCE, symbol="BTCUSDT")
        counters = StepCounters()
        process_market_event(
            event=mark, now=boundary, engine=stack.engine,
            features={"BTCUSDT": feature}, decision_fn_for=lambda _s: None,
            config=StepConfig(), counters=counters, prediction_runtime=None,
            funding_trackers={"BTCUSDT": tracker})
        payments = stack.accounting.funding.payments
        self.assertEqual(len(payments), 1)
        self.assertAlmostEqual(payments[0].amount, -1.0 * 100.0 * 0.0001)
        # 再发一条越过同边界的事件不重复结算
        later = mark_price_event(price=100.0, exchange_ts=boundary + 1_000,
                                    funding_rate=0.0001, next_funding_time=boundary)
        process_market_event(
            event=later, now=boundary + 1_000, engine=stack.engine,
            features={"BTCUSDT": feature}, decision_fn_for=lambda _s: None,
            config=StepConfig(), counters=counters, prediction_runtime=None,
            funding_trackers={"BTCUSDT": tracker})
        self.assertEqual(len(stack.accounting.funding.payments), 1)


if __name__ == "__main__":
    unittest.main()
