"""P0017.5：实时多标的行情驱动**模拟**交易链路的确定性端到端验收。

与真实网络无关：用脚本化 `BatchSource` 驱动 `LiveMultiSymbolRunner`，但走**真实**
FeatureEngine / MakerPolicy / ExecutionEngine（P0017.2 组合闸）/ 多个 SimulatedVenue /
AccountingCore——没有 mock 执行、没有伪造成交。

覆盖提案 §4：
- A 端到端：≥2 标的真实决策→模拟订单/成交/会计；跨标的组合预算 + 同批次竞争；风险拒绝无副作用；
  单标的故障隔离；正常停止与挂单撤销验证；双跑确定性/因果；可审计状态。
- B 结构/安全：离线/实时共享步进核心；模拟路径不依赖 testnet/写 connector（AST 守卫在
  tests/unit/test_live_paper_isolation.py）；未知标的/缺状态/无效配置明确失败；风险/订单/成交/会计一致。
"""

from __future__ import annotations

import ast
import pathlib
import tempfile
import unittest
from collections import defaultdict
from typing import Callable

from backtest.prediction import local_trial_runtime
from runtime.live_multi_symbol import (
    LiveMultiSymbolRunner,
    LiveRiskConfig,
    LiveRunError,
    RunStatus,
    SymbolBatch,
)
from runtime.maker_config import build_maker_policy

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYMBOLS = ("BTCUSDT", "ETHUSDT")


def _maker_values(tick: float, step: float) -> dict:
    profile = (PROJECT_ROOT / "profiles" / "backtest-default.json").read_text(encoding="utf-8")
    import json
    v = json.loads(profile)
    v["strategy.maker.tick_size"] = tick
    v["strategy.maker.quantity_step"] = step
    v["venue.rules.tick_size"] = str(tick)
    v["venue.rules.step_size"] = str(step)
    return v


def _load_fixture_events():
    """复用 P0017.4 已验证的触价成交微观结构（含 mark），按 symbol 分组的泵序列。"""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "tmsb_fixture", str(PROJECT_ROOT / "tests/integration/test_multi_symbol_backtest.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tmsb_fixture"] = mod
    spec.loader.exec_module(mod)
    btc = mod._build_symbol_events("BTCUSDT", base_price=60_000, tick=0.1, steps=300,
                                      seed=11, mark_price=60_000.0)
    eth = mod._build_symbol_events("ETHUSDT", base_price=3_000, tick=0.01, steps=300,
                                      seed=22, mark_price=3_000.0)
    merged = mod._merge_time_order(btc, eth)
    groups: dict[int, dict[str, list]] = defaultdict(lambda: {"BTCUSDT": [], "ETHUSDT": []})
    for e in merged:
        groups[e.exchange_ts][e.symbol].append(e)
    return [ts for ts in sorted(groups)], groups


class ScriptedSource:
    """脚本化实时源：每个 pump 交付一个时间戳下各 symbol 的批次（确定性）。"""

    symbols = SYMBOLS

    def __init__(self, ordered_ts, groups, *, fail_symbols: dict[int, str] | None = None,
                 extra_events: dict[int, list] | None = None) -> None:
        self._ts = ordered_ts
        self._groups = groups
        self._fail = fail_symbols or {}
        self._extra = extra_events or {}
        self.i = 0
        self.closed = False

    def pump(self) -> tuple[SymbolBatch, ...]:
        if self.i >= len(self._ts):
            return ()
        ts = self._ts[self.i]
        self.i += 1
        out = []
        for sym in SYMBOLS:
            if self._fail.get(self.i - 1) == sym:
                out.append(SymbolBatch(symbol=sym, events=(), source_error="injected_ws_error"))
            else:
                out.append(SymbolBatch(symbol=sym, events=tuple(self._groups[ts][sym])))
        for ev in self._extra.get(self.i - 1, ()):
            out.append(ev if isinstance(ev, SymbolBatch) else
                        SymbolBatch(ev[0], (ev[1],)))
        return tuple(out)

    def close(self) -> None:
        self.closed = True


def _policies() -> dict:
    return {"BTCUSDT": build_maker_policy(_maker_values(0.1, 0.0001)),
            "ETHUSDT": build_maker_policy(_maker_values(0.01, 0.0001))}


def _risk(**over) -> LiveRiskConfig:
    base = dict(initial_balance=10_000_000.0, maker_fee_rate=0.0002, fee_asset="USDT",
                 submit_latency_ms=0, cancel_latency_ms=0,
                 max_position_notional=1_000_000.0, max_open_order_exposure=1_000_000.0,
                 max_portfolio_gross_notional=1_000_000.0)
    base.update(over)
    return LiveRiskConfig(**base)


def _runner(source, *, risk=None, policies=None, prediction=True, run_id="live-paper"):
    return LiveMultiSymbolRunner(
        symbols=SYMBOLS, source=source, risk=risk or _risk(),
        decision_fn_for=(lambda s: (policies or _policies())[s]),
        prediction_runtime=(local_trial_runtime() if prediction else None),
        run_id=run_id, max_pumps=source.i_max if hasattr(source, "i_max") else None)


class LiveMultiSymbolPaperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ordered_ts, cls.groups = _load_fixture_events()

    def _source(self, **kw) -> ScriptedSource:
        s = ScriptedSource(self.ordered_ts, self.groups, **kw)
        s.i_max = len(self.ordered_ts)
        return s

    # ------------------------------------------------------------ A 端到端

    def test_e2e_real_decisions_produce_simulated_orders_fills_and_accounting(self) -> None:
        """A1/A2/A8：真实策略在实时链路上产生模拟订单、成交与会计更新，并有可审计状态。"""
        runner = _runner(self._source())
        stats = runner.run_forever(pump_interval_s=0)
        self.assertEqual(stats.status, RunStatus.STOPPED)
        self.assertGreater(stats.counters.submits, 0, "应有真实策略驱动的模拟下单")
        self.assertGreater(stats.counters.fills, 0, "应有触价模拟成交")
        # 订单确实进 tracker（非函数调用/mock），且只属于声明标的
        syms = {o.symbol for o in runner.tracker.orders}
        self.assertEqual(syms, set(SYMBOLS))
        # 会计：成交入 FillLedger，权益/持仓发生真实变化，权益有限
        self.assertGreater(runner.accounting.fills.count, 0)
        self.assertTrue(__import__("math").isfinite(runner.accounting.equity()))
        self.assertNotAlmostEqual(runner.accounting.equity(), 10_000_000.0, places=2)
        view = stats.view()
        self.assertEqual(view["status"], "STOPPED")
        self.assertIn("symbols", view)
        self.assertEqual(set(view["symbols"]), set(SYMBOLS))

    def test_portfolio_budget_is_enforced_across_symbols_live(self) -> None:
        """A3：实时路径跨标的组合预算真实生效（小限额 ⇒ PORTFOLIO_NOTIONAL_LIMIT 拒绝）。"""
        runner = _runner(self._source(),
                          risk=_risk(max_portfolio_gross_notional=500.0))
        stats = runner.run_forever(pump_interval_s=0)
        reasons = {r["reason"] for r in stats.counters.rejects_log}
        self.assertIn("PORTFOLIO_NOTIONAL_LIMIT", reasons)
        # accepted 非 reduce-only OPEN 的 projected gross 不得超 500
        open_new = sum(o.notional for o in runner.tracker.orders
                        if o.status.value == "OPEN" and not o.reduce_only)
        self.assertLessEqual(open_new, 500.0 + 1e-9)

    def test_risk_rejection_has_no_order_or_accounting_side_effect(self) -> None:
        """A4：被组合闸拒的候选不进 tracker、无成交、不改动会计。"""
        # 极小限额 ⇒ 所有新增暴露都被拒
        runner = _runner(self._source(),
                          risk=_risk(max_portfolio_gross_notional=1e-9))
        stats = runner.run_forever(pump_interval_s=0)
        self.assertGreater(stats.counters.risk_rejects, 0)
        self.assertEqual(len(runner.tracker.orders), 0, "拒绝候选不得注册为订单")
        self.assertEqual(runner.accounting.fills.count, 0)
        self.assertAlmostEqual(runner.accounting.equity(), 10_000_000.0, places=6)

    def test_single_symbol_failure_does_not_stop_healthy_symbol(self) -> None:
        """A5：某标的采集错误被隔离计数，另一标的继续决策/成交。"""
        # 在中段让 ETH 连续若干泵报错（i 在 pump() 内自增，fail 键=自增后值）
        fail = {i: "ETHUSDT" for i in range(81, 91)}
        source = self._source(fail_symbols=fail)
        runner = _runner(source)
        stats = runner.run_forever(pump_interval_s=0)
        self.assertEqual(stats.status, RunStatus.STOPPED)
        eth = stats.health["ETHUSDT"]
        self.assertGreaterEqual(eth.error_count, 10)
        self.assertTrue(eth.had_failure, "ETH 采集故障应被粘滞记录")
        # BTC 仍健康且有事件/订单
        self.assertTrue(stats.health["BTCUSDT"].healthy)
        btc_orders = [o for o in runner.tracker.orders if o.symbol == "BTCUSDT"]
        self.assertGreater(len(btc_orders), 0)

    def test_graceful_stop_cancels_resting_orders_and_verifies_terminal(self) -> None:
        """A6：停止时撤销 active 模拟挂单并验证终态；不强平、不虚构收盘、无残留 active。"""
        runner = _runner(self._source())
        stats = runner.run_forever(pump_interval_s=0)
        self.assertEqual(stats.status, RunStatus.STOPPED)
        # 有挂单被请求撤销，且全部确认（open_after_stop 为空 = 无假定撤销）
        self.assertGreater(stats.stop_cancel_requested, 0)
        self.assertEqual(stats.stop_cancel_confirmed, stats.stop_cancel_requested)
        self.assertEqual(stats.open_after_stop, ())
        self.assertEqual(len(runner.tracker.active()), 0)

    def test_repeated_runs_are_deterministic(self) -> None:
        """A7：同样的脚本化输入双跑，订单/成交/拒绝轨迹逐一致。"""
        def run_once(rid):
            runner = _runner(self._source(), run_id=rid)
            st = runner.run_forever(pump_interval_s=0)
            orders = [(o.symbol, o.status.value, round(o.notional, 8), o.reduce_only)
                       for o in runner.tracker.orders]
            equity = runner.accounting.equity()
            fills = runner.accounting.fills.count
            return orders, st.counters.rejects_log, st.view(), equity, fills
        a = run_once("det-1")
        b = run_once("det-2")
        self.assertEqual(a[0], b[0])      # 订单
        self.assertEqual(a[1], b[1])      # 拒绝轨迹
        self.assertEqual(a[3], b[3])      # 权益
        self.assertEqual(a[4], b[4])      # 成交数

    # ------------------------------------------------------------ B 结构/安全

    def test_same_batch_budget_contention_winner_is_deterministic_live(self) -> None:
        """A3：同一批次两个标的竞争稀缺组合预算时，按 (ts,symbol,arrival) 确定性胜出。"""
        from strategy.maker.types import (MakerDecision, QuoteAction, QuoteDecision,
                                            QuoteTrigger, Side as MktSide)
        from risk.types import OrderProposal, Side as RiskSide
        from tests.support import depth_snapshot_event, mark_price_event
        STEP = 5_000
        base = 1_700_000_000_000

        def book(sym, mid, ts, uid):
            return depth_snapshot_event(
                uid, symbol=sym,
                bids=[(round(mid - 0.1 - o * 0.1, 2), 5.0) for o in range(5)],
                asks=[(round(mid + 0.1 + o * 0.1, 2), 5.0) for o in range(5)],
                exchange_ts=ts, receive_ts=ts, process_ts=ts)

        events: list = []
        uid = [1]

        def nid():
            uid[0] += 1
            return uid[0]

        for i in range(80):
            ts = base + i * STEP
            events.append(mark_price_event(symbol="BTCUSDT", price=60_000, exchange_ts=ts,
                                              receive_ts=ts, process_ts=ts))
            events.append(book("BTCUSDT", 60_000, ts, nid()))
            events.append(mark_price_event(symbol="ETHUSDT", price=3_000, exchange_ts=ts,
                                              receive_ts=ts, process_ts=ts))
            events.append(book("ETHUSDT", 3_000, ts, nid()))
        groups: dict[int, dict[str, list]] = defaultdict(lambda: {"BTCUSDT": [], "ETHUSDT": []})
        for e in events:
            groups[e.exchange_ts][e.symbol].append(e)
        ts_sorted = sorted(groups)

        decided: set[str] = set()

        def place_once(sym, price):
            def fn(*, state, prediction, position, snapshot, existing_orders,
                     remaining_risk_budget, kill_switch):
                if sym in decided:
                    none = QuoteDecision(side=MktSide.SELL, action=QuoteAction.NONE,
                                            trigger=QuoteTrigger.INITIAL_QUOTE)
                    return MakerDecision(symbol=sym, at_ms=state.time.as_of_exchange_ts,
                                            bid=QuoteDecision(side=MktSide.BUY,
                                                                action=QuoteAction.NONE,
                                                                trigger=QuoteTrigger.INITIAL_QUOTE),
                                            ask=none, bid_desired=False, ask_desired=False,
                                            decision_id=f"d-{sym}")
                decided.add(sym)
                prop = OrderProposal(symbol=sym, side=RiskSide.BUY, quantity=400.0 / price,
                                        price=price, reduce_only=False, post_only=True)
                return MakerDecision(
                    symbol=sym, at_ms=state.time.as_of_exchange_ts,
                    bid=QuoteDecision(side=MktSide.BUY, action=QuoteAction.PLACE,
                                        trigger=QuoteTrigger.INITIAL_QUOTE, proposal=prop),
                    ask=QuoteDecision(side=MktSide.SELL, action=QuoteAction.NONE,
                                        trigger=QuoteTrigger.INITIAL_QUOTE),
                    bid_desired=True, ask_desired=False, decision_id=f"d-{sym}")
            return fn

        fns = {"BTCUSDT": place_once("BTCUSDT", 60_000),
                 "ETHUSDT": place_once("ETHUSDT", 3_000)}

        def run_once():
            decided.clear()
            src = ScriptedSource(ts_sorted, groups)
            src.i_max = len(ts_sorted)
            runner = LiveMultiSymbolRunner(
                symbols=SYMBOLS, source=src, risk=_risk(max_portfolio_gross_notional=500.0),
                decision_fn_for=lambda s: fns[s], prediction_runtime=None,
                run_id="contend", max_pumps=len(ts_sorted))
            stats = runner.run_forever(pump_interval_s=0)
            return runner, stats

        runner, stats = run_once()
        # 恰好一个标的的新增暴露单被接受（BTC，名义 400）；实时停止会把挂单撤销并验证终态，
        # 因此其终态为 CANCELED（stop_cancel_confirmed），重点是没有第二个标的的单被接受。
        accepted = [(o.symbol, o.notional) for o in runner.tracker.orders]
        self.assertEqual(accepted, [("BTCUSDT", 400.0)])
        self.assertEqual({o.symbol for o in runner.tracker.orders}, {"BTCUSDT"})
        self.assertEqual(stats.open_after_stop, ())
        self.assertEqual(stats.stop_cancel_confirmed, stats.stop_cancel_requested)
        eth_rejects = [r for r in stats.counters.rejects_log if r["symbol"] == "ETHUSDT"]
        self.assertTrue(eth_rejects)
        self.assertTrue(all(r["reason"] == "PORTFOLIO_NOTIONAL_LIMIT" for r in eth_rejects))
        self.assertEqual(runner.accounting.fills.count, 0)
        # 双跑一致（确定性胜出）
        runner2, stats2 = run_once()
        self.assertEqual([(o.symbol, o.notional) for o in runner2.tracker.orders], accepted)
        self.assertEqual(stats2.counters.rejects_log, stats.counters.rejects_log)

    def test_unknown_symbol_batch_is_counted_not_silently_dropped_or_routed(self) -> None:
        """B3：源给出未登记标的 ⇒ 计数 unknown，不猜测归属，不影响已登记标的。"""
        from tests.support import depth_snapshot_event
        ev = depth_snapshot_event(999_001, symbol="SOLUSDT",
                                    bids=[(100.0, 1.0)], asks=[(100.1, 1.0)],
                                    exchange_ts=self.ordered_ts[10], receive_ts=self.ordered_ts[10],
                                    process_ts=self.ordered_ts[10])
        source = self._source(extra_events={10: [SymbolBatch("SOLUSDT", (ev,))]})
        runner = _runner(source)
        stats = runner.run_forever(pump_interval_s=0)
        self.assertGreaterEqual(stats.unknown_symbol_events, 1)
        # 无 SOL 订单/状态
        self.assertFalse(any(o.symbol == "SOLUSDT" for o in runner.tracker.orders))

    def test_out_of_order_event_is_skipped_and_counted(self) -> None:
        """乱序（exchange_ts 严格回退）事件被跳过计数，不污染因果（不使用旧/未来事件决策）。"""
        from tests.support import depth_snapshot_event
        late = self.ordered_ts[100]
        early = self.ordered_ts[20]  # 明显更旧
        stale = depth_snapshot_event(999_002, symbol="BTCUSDT",
                                        bids=[(60_000.0, 1.0)], asks=[(60_000.1, 1.0)],
                                        exchange_ts=early, receive_ts=early, process_ts=early)
        # 在第 101 泵（时间已到 late）追加一条 ts=early 的旧事件
        source = self._source(extra_events={100: [SymbolBatch("BTCUSDT", (stale,))]})
        runner = _runner(source)
        stats = runner.run_forever(pump_interval_s=0)
        self.assertGreaterEqual(stats.health["BTCUSDT"].out_of_order_skipped, 1)

    def test_invalid_configuration_fails_fast(self) -> None:
        """B3：<2 标的、重复标的、源不提供标的、非法余额 ⇒ 明确 LiveRunError。"""
        class S(ScriptedSource):
            pass
        with self.assertRaises(LiveRunError):
            LiveMultiSymbolRunner(symbols=("BTCUSDT",), source=self._source(), risk=_risk(),
                                    decision_fn_for=lambda s: None)
        with self.assertRaises(LiveRunError):
            LiveMultiSymbolRunner(symbols=("BTCUSDT", "BTCUSDT"), source=self._source(),
                                    risk=_risk(), decision_fn_for=lambda s: None)
        with self.assertRaises(LiveRunError):
            LiveMultiSymbolRunner(symbols=("BTCUSDT", "SOLUSDT"), source=self._source(),
                                    risk=_risk(), decision_fn_for=lambda s: None)
        with self.assertRaises(LiveRunError):
            LiveRiskConfig(initial_balance=0.0, maker_fee_rate=0.0, fee_asset="USDT")

    def test_offline_and_live_share_single_step_implementation(self) -> None:
        """B1：结构断言——离线 multi_driver 与实时 runner 都引用共享步进核心（无两份实现）。"""
        import backtest.multi_driver as md
        import runtime.live_multi_symbol as live
        self.assertTrue(hasattr(md, "process_market_event"))
        self.assertIs(md.process_market_event, live.process_market_event)
        self.assertIs(md.build_commitment_provider, live.build_commitment_provider)


if __name__ == "__main__":
    unittest.main()
