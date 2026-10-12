"""P0017.4：多标的确定性组合回测（状态隔离、组合预算、会计核对、确定性）。"""

from __future__ import annotations

import json
import pathlib
import random
import shutil
import tempfile
import unittest

from backtest import BacktestStatus
from backtest.identity import dataset_identity, run_identity
from backtest.multi_driver import MultiBacktestConfig, run_multi_backtest
from backtest.prediction import local_trial_runtime
from runtime.maker_config import build_maker_policy
from tests.support import (
    AggressorSide,
    BASE_TS,
    depth_snapshot_event,
    mark_price_event,
    trade_event,
    write_store,
)

PROFILE = (pathlib.Path(__file__).resolve().parents[2] / "profiles" / "backtest-default.json")
STEP = 5_000


def _maker_values(tick: float, step: float) -> dict:
    v = json.loads(PROFILE.read_text(encoding="utf-8"))
    v["strategy.maker.tick_size"] = tick
    v["strategy.maker.quantity_step"] = step
    v["venue.rules.tick_size"] = str(tick)
    v["venue.rules.step_size"] = str(step)
    return v


def _build_symbol_events(symbol: str, *, base_price: float, tick: float,
                          steps: int, seed: int, start_offset: int = 0,
                          mark_price: float | None = None) -> list:
    """构造一个 symbol 的确定性 trade+snapshot（+mark）事件。

    复用已验证的 `build_market_events`（触价成交微观结构，保证 SimulatedVenue 可成交），
    retag 为目标 symbol。各 SimulatedVenue 独立去重，故不同 symbol 的事件 id 重复无影响。
    """
    from tests.ui.market_fixture import build_market_events
    hours = max(1, (steps * STEP) // 3_600_000)
    raw = build_market_events(hours=hours, step_ms=STEP, seed=seed,
                                mark_price=(60_000.0 if mark_price is not None else None))
    for event in raw:
        object.__setattr__(event, "symbol", symbol)
    unique_ts: list[int] = []
    for event in raw:
        if event.exchange_ts not in unique_ts:
            unique_ts.append(event.exchange_ts)
    keep = set(unique_ts[:steps])
    return [e for e in raw if e.exchange_ts in keep]


def _merge_time_order(*groups) -> list:
    merged = [e for group in groups for e in group]
    merged.sort(key=lambda e: (e.exchange_ts, e.symbol))
    return merged


class MultiBacktestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-multi-"))
        self.store = self.tmp / "events.jsonl"
        btc = _build_symbol_events("BTCUSDT", base_price=60_000, tick=0.1, steps=300, seed=11,
                                     mark_price=60_000.0)
        eth = _build_symbol_events("ETHUSDT", base_price=3_000, tick=0.01, steps=300, seed=22,
                                     mark_price=3_000.0)
        write_store(self.store, _merge_time_order(btc, eth))
        self.identity = dataset_identity([self.store])
        self.values_btc = _maker_values(0.1, 0.0001)
        self.values_eth = _maker_values(0.01, 0.0001)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, **over) -> MultiBacktestConfig:
        base = dict(
            symbol="BTCUSDT", symbols=("BTCUSDT", "ETHUSDT"),
            initial_balance=10_000_000.0, maker_fee_rate=0.0002, fee_asset="USDT",
            submit_latency_ms=0, cancel_latency_ms=0,
            max_position_notional=1_000_000.0, max_open_order_exposure=1_000_000.0)
        base.update(over)
        return MultiBacktestConfig(**base)

    def _policies(self) -> dict:
        return {"BTCUSDT": build_maker_policy(self.values_btc),
                "ETHUSDT": build_maker_policy(self.values_eth)}

    def _run(self, *, config=None, prediction=True):
        cfg = config or self._cfg()
        pred = local_trial_runtime() if prediction else None
        run_id, _ = run_identity(code_version="test", dataset_sha256=self.identity["sha256"],
                                   config=cfg.canonical(), seed=cfg.seed)
        return run_multi_backtest(self.store, cfg, run_id=run_id,
                                    dataset_sha256=self.identity["sha256"],
                                    decision_fn=self._policies(), prediction_runtime=pred)

    def test_two_symbols_run_in_one_backtest_with_isolation(self) -> None:
        result = self._run()
        self.assertEqual(result.status, BacktestStatus.SUCCESS, result.reason)
        symbols = {o.symbol for o in result.tracker.orders}
        self.assertEqual(symbols, {"BTCUSDT", "ETHUSDT"})
        # 每个 symbol 的订单只属于自己（无跨标的污染）
        for order in result.tracker.orders:
            self.assertIn(order.symbol, ("BTCUSDT", "ETHUSDT"))
        self.assertGreater(result.trace.events, 0)

    def test_portfolio_budget_blocks_cross_symbol_accumulation(self) -> None:
        # 组合限额设很小 ⇒ 新增暴露订单的累计名义不得超限（C03 端到端）。
        # 注意：reduce-only 挂单按 P0017.2 语义可越过 gross 上限（它们只能降低净敞口），
        # 因此正确不变式是"新增暴露（非 reduce-only）订单 projected gross ≤ limit"。
        cfg = self._cfg(max_position_notional=1_000_000.0, max_open_order_exposure=1_000_000.0,
                         max_portfolio_gross_notional=500.0)
        result = self._run(config=cfg)
        self.assertEqual(result.status, BacktestStatus.SUCCESS)
        reasons = {r["reason"] for r in result.trace.rejects_log}
        self.assertIn("PORTFOLIO_NOTIONAL_LIMIT", reasons)
        # 被拒单不进 tracker；只有 accepted 非 reduce-only OPEN 计入新增暴露
        new_open = [o for o in result.tracker.orders
                      if o.status.value == "OPEN" and not o.reduce_only]
        self.assertLessEqual(sum(o.notional for o in new_open), 500.0 + 1e-6)
        # 终态净持仓几乎平掉（reduce-only 挂单不会凭空扩大净敞口）
        net = sum(abs(p.qty) for p in result.accounting.positions)
        self.assertLess(net, 0.01)

    def test_rejected_orders_have_no_accounting_side_effect(self) -> None:
        cfg = self._cfg(max_portfolio_gross_notional=200.0)
        result = self._run(config=cfg)
        # 所有成交都对应 tracker 中的接受订单；fills 数量非负且 ledger 一致
        self.assertEqual(result.accounting.fills.count, result.trace.fills)
        for fill in result.accounting.fills.fills:
            self.assertIn(fill.symbol, ("BTCUSDT", "ETHUSDT"))

    def test_unknown_symbol_in_data_is_rejected(self) -> None:
        rogue = self.tmp / "rogue.jsonl"
        events = _build_symbol_events("BTCUSDT", base_price=60_000, tick=0.1, steps=10, seed=11,
                                        mark_price=60_000.0)
        events.append(trade_event(999, symbol="SOLUSDT", price=100, quantity=1,
                                    aggressor=AggressorSide.BUY,
                                    exchange_ts=BASE_TS + 999 * STEP))
        write_store(rogue, events)
        cfg = self._cfg()
        with self.assertRaises(Exception) as ctx:
            run_multi_backtest(rogue, cfg, run_id="x",
                                 dataset_sha256=dataset_identity([rogue])["sha256"],
                                 decision_fn=self._policies())
        self.assertEqual(ctx.exception.status, BacktestStatus.INVALID_INPUT)

    def test_declared_symbol_absent_from_data_fails_integrity(self) -> None:
        cfg = self._cfg(symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
        # 数据只有 BTC/ETH，声明了 SOL ⇒ 不抛异常但完整性检查失败（INTEGRITY_FAILURE）
        from backtest.identity import run_identity
        run_id, _ = run_identity(code_version="t", dataset_sha256=self.identity["sha256"],
                                   config=cfg.canonical(), seed=cfg.seed)
        result = run_multi_backtest(self.store, cfg, run_id=run_id,
                                       dataset_sha256=self.identity["sha256"],
                                       decision_fn=self._policies(),
                                       prediction_runtime=local_trial_runtime())
        self.assertEqual(result.status, BacktestStatus.INTEGRITY_FAILURE)
        self.assertIn("every_declared_symbol_present_in_data", result.reason)

    def test_reproducible_two_runs(self) -> None:
        r1 = self._run()
        r2 = self._run()
        def orders(r):
            return [(o.symbol, o.client_order_id, o.status.value, round(o.filled_quantity, 10),
                       round(o.avg_fill_price, 8)) for o in r.tracker.orders]
        self.assertEqual(orders(r1), orders(r2))
        self.assertEqual(r1.accounting.equity(), r2.accounting.equity())
        self.assertEqual(r1.trace, r2.trace)

    def test_future_event_preserves_prefix(self) -> None:
        cfg = self._cfg()
        ident = self.identity
        rid, _ = run_identity(code_version="t", dataset_sha256=ident["sha256"],
                                config=cfg.canonical(), seed=cfg.seed)
        base = run_multi_backtest(self.store, cfg, run_id=rid, dataset_sha256=ident["sha256"],
                                    decision_fn=self._policies(), prediction_runtime=local_trial_runtime())
        # 追加一个 ETH 未来事件
        ext = self.tmp / "ext.jsonl"
        ext.write_text(self.store.read_text(encoding="utf-8"), encoding="utf-8")
        from storage.events.writer import JsonlEventWriter
        with JsonlEventWriter(ext) as w:
            w.append(trade_event(999_999, symbol="ETHUSDT", price=3_001, quantity=1,
                                   aggressor=AggressorSide.BUY,
                                   exchange_ts=BASE_TS + 400 * STEP))
        ext_ident = dataset_identity([ext])
        ext_run = run_multi_backtest(ext, cfg, run_id=rid, dataset_sha256=ext_ident["sha256"],
                                        decision_fn=self._policies(),
                                        prediction_runtime=local_trial_runtime())
        def content(r):
            return [(o.symbol, o.client_order_id, o.status.value, round(o.filled_quantity, 10),
                       round(o.avg_fill_price, 8)) for o in r.tracker.orders]
        ob, oe = content(base), content(ext_run)
        self.assertEqual(ob, oe[: len(ob)])
        self.assertEqual(base.accounting.equity(), ext_run.accounting.equity())
        self.assertEqual(base.accounting.fills.count, ext_run.accounting.fills.count)

    def test_same_timestamp_budget_contention_winner_is_deterministic(self) -> None:
        """C-2：同一时间戳两个标的竞争有限组合预算时，按 (exchange_ts, ordinal) 契约
        确定性胜出——store 中先出现的标的接受，后出现者被组合闸拒；拒绝无执行/会计副作用。"""
        from strategy.maker.types import (
            MakerDecision, QuoteAction, QuoteDecision, QuoteTrigger,
        )
        from strategy.maker.types import Side as MktSide
        from risk.types import OrderProposal, Side as RiskSide

        store = self.tmp / "contention.jsonl"
        events: list = []
        uid = 1
        ts0 = BASE_TS

        def book(symbol: str, mid: float, ts: int, ident: int):
            bids = [(round(mid - 0.1 - o * 0.1, 2), 5.0) for o in range(5)]
            asks = [(round(mid + 0.1 + o * 0.1, 2), 5.0) for o in range(5)]
            return depth_snapshot_event(ident, symbol=symbol, bids=bids, asks=asks,
                                          exchange_ts=ts, receive_ts=ts, process_ts=ts)

        # 预热：足够让两标的 FeatureEngine 变 tradeable（含 mark）
        for i in range(150):
            ts = ts0 + i * STEP
            events.append(mark_price_event(symbol="BTCUSDT", price=60_000,
                                              exchange_ts=ts, receive_ts=ts, process_ts=ts))
            events.append(book("BTCUSDT", 60_000, ts, uid)); uid += 1
            events.append(mark_price_event(symbol="ETHUSDT", price=3_000,
                                              exchange_ts=ts, receive_ts=ts, process_ts=ts))
            events.append(book("ETHUSDT", 3_000, ts, uid)); uid += 1
        # 在最后一个时间戳，两标的都收到 book（store 顺序 BTC 块 → ETH 块，ordinal 决定胜出）
        final_ts = ts0 + 150 * STEP
        events.append(mark_price_event(symbol="BTCUSDT", price=60_000,
                                          exchange_ts=final_ts, receive_ts=final_ts, process_ts=final_ts))
        events.append(book("BTCUSDT", 60_000, final_ts, uid)); uid += 1
        events.append(mark_price_event(symbol="ETHUSDT", price=3_000,
                                          exchange_ts=final_ts, receive_ts=final_ts, process_ts=final_ts))
        events.append(book("ETHUSDT", 3_000, final_ts, uid)); uid += 1
        write_store(store, events)

        # 每个 symbol 只决策一次：400 名义新增暴露 BUY；组合限额 500，只够一个
        decided: set[str] = set()

        def place_once(symbol: str, price: float):
            def fn(*, state, prediction, position, snapshot, existing_orders,
                    remaining_risk_budget, kill_switch):
                if symbol in decided:
                    no = QuoteDecision(side=MktSide.SELL, action=QuoteAction.NONE,
                                          trigger=QuoteTrigger.INITIAL_QUOTE)
                    return MakerDecision(symbol=symbol, at_ms=snapshot.now_ms,
                                            bid=QuoteDecision(side=MktSide.BUY,
                                                                action=QuoteAction.NONE,
                                                                trigger=QuoteTrigger.INITIAL_QUOTE),
                                            ask=no, bid_desired=False, ask_desired=False,
                                            decision_id=f"d-{symbol}")
                decided.add(symbol)
                prop = OrderProposal(symbol=symbol, side=RiskSide.BUY, quantity=400.0 / price,
                                        price=price, reduce_only=False, post_only=True)
                bid = QuoteDecision(side=MktSide.BUY, action=QuoteAction.PLACE,
                                       trigger=QuoteTrigger.INITIAL_QUOTE, proposal=prop)
                ask = QuoteDecision(side=MktSide.SELL, action=QuoteAction.NONE,
                                       trigger=QuoteTrigger.INITIAL_QUOTE)
                return MakerDecision(symbol=symbol, at_ms=snapshot.now_ms, bid=bid, ask=ask,
                                        bid_desired=True, ask_desired=False, decision_id=f"d-{symbol}")
            return fn

        cfg = self._cfg(max_portfolio_gross_notional=500.0)
        ident = dataset_identity([store])

        def run_once():
            decided.clear()
            rid, _ = run_identity(code_version="t", dataset_sha256=ident["sha256"],
                                    config=cfg.canonical(), seed=cfg.seed)
            return run_multi_backtest(store, cfg, run_id=rid, dataset_sha256=ident["sha256"],
                                         decision_fn={"BTCUSDT": place_once("BTCUSDT", 60_000),
                                                         "ETHUSDT": place_once("ETHUSDT", 3_000)},
                                         prediction_runtime=None)

        result = run_once()
        self.assertEqual(result.status, BacktestStatus.SUCCESS, result.reason)
        # 只有一笔订单进入 tracker（BTC：store 同 ts 中 ordinal 更小）
        accepted = [(o.symbol, o.status.value, o.notional) for o in result.tracker.orders]
        self.assertEqual(accepted, [("BTCUSDT", "OPEN", 400.0)])
        # ETH 被组合闸拒（不是其他原因），且没有任何成交/会计副作用
        eth_rejects = [r for r in result.trace.rejects_log if r["symbol"] == "ETHUSDT"]
        self.assertTrue(eth_rejects)
        self.assertTrue(all(r["reason"] == "PORTFOLIO_NOTIONAL_LIMIT" for r in eth_rejects))
        self.assertEqual(result.accounting.fills.count, 0)
        self.assertEqual(result.accounting.equity(), cfg.initial_balance)
        # 双跑确定性：胜出者与轨迹一致
        again = run_once()
        self.assertEqual([(o.symbol, o.status.value, o.notional) for o in again.tracker.orders],
                          accepted)
        self.assertEqual(again.trace.rejects_log, result.trace.rejects_log)


if __name__ == "__main__":
    unittest.main()
