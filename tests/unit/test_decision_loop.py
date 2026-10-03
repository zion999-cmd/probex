"""P0001.14：决策 loop 的编排行为（策略/风险/执行全部由既有 Owner 实现，本层只编排）。"""

from __future__ import annotations

import threading
import unittest
from unittest import mock
from types import SimpleNamespace
from typing import Any

from portfolio.types import Side
from product.types import RuntimeMode
from risk.types import OrderProposal
from runtime import decision_loop as decision_loop_module
from runtime.decision_loop import (PAPER_READINESS_REASON, DecisionLoopConfig, DecisionLoopError,
                                   RuntimeDecisionLoop)
from strategy.maker.types import MakerDecision, QuoteAction, QuoteDecision, QuoteTrigger


class _Engine:
    """ExecutionEngine 的 duck-typed 替身（真 RiskGate 在真实 engine 内部，不在此测试范围内）。"""

    def __init__(self, *, reject: bool = False) -> None:
        self.submitted: list[Any] = []
        self.cancelled: list[str] = []
        self.polled: list[int] = []
        self.rejections: list[Any] = []
        self.accounting = SimpleNamespace(position=lambda symbol: None)
        self._reject = reject

    def snapshot(self, symbol: str, *, now_ms: int) -> Any:
        return SimpleNamespace(symbol=symbol, now_ms=now_ms)

    def submit(self, proposal: Any, *, now_ms: int) -> Any:
        self.submitted.append(proposal)
        if self._reject:
            self.rejections.append(SimpleNamespace(reason_code=None))
            return SimpleNamespace(submitted=False, rejected=True)
        return SimpleNamespace(submitted=True, rejected=False)

    def cancel(self, client_order_id: str, *, now_ms: int) -> Any:
        self.cancelled.append(client_order_id)
        return SimpleNamespace(submitted=True)

    def poll(self, *, now_ms: int) -> Any:
        self.polled.append(now_ms)
        return SimpleNamespace()


class _Tracker:
    def __init__(self, *, terminal_after_cancel: bool = True) -> None:
        self._active: tuple[Any, ...] = ()
        self._orders: dict[str, Any] = {}
        self._terminal_after_cancel = terminal_after_cancel
        self._settle: set[str] = set()

    def active(self) -> tuple[Any, ...]:
        return self._active

    def order(self, client_order_id: str) -> Any:
        if client_order_id in self._settle:
            return SimpleNamespace(is_terminal=True)
        return self._orders.get(client_order_id)


class _Policy:
    def __init__(self, decision: Any) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def decide(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.decision


class _PredictionRuntime:
    """PredictionRuntime 的 duck-typed 替身：`submit` 与真实实现一样产出并记住新记录。"""

    def __init__(self, record: Any = None, *, expired: bool = False) -> None:
        self.record = record
        self.latest_prediction: Any | None = None
        self.next_retry_at: int | None = None
        self.submitted: list[Any] = []
        self._expired = expired

    async def submit(self, state: Any) -> Any:
        self.submitted.append(state)
        self._expired = False
        self.latest_prediction = self.record
        return SimpleNamespace(record=self.record)

    def is_expired(self, record: Any) -> bool:
        return self._expired


def state(*, tradeable: bool = True, marker: str = "s1") -> Any:
    return SimpleNamespace(marker=marker, quality=SimpleNamespace(tradeable=tradeable))


def quote(side: Side, action: QuoteAction, *, client_order_id: str | None = None) -> QuoteDecision:
    """按既有契约构造某一侧动作：PLACE 带 proposal；KEEP/CANCEL/REPLACE 带既有 order id。"""
    kwargs: dict[str, Any] = {}
    if action in (QuoteAction.PLACE, QuoteAction.REPLACE):
        kwargs.update(proposal=OrderProposal(symbol="BTCUSDT", side=side, quantity=0.1,
                                            price=100.0, post_only=True),
                      price=100.0, quantity=0.1)
    if action in (QuoteAction.KEEP, QuoteAction.CANCEL, QuoteAction.REPLACE):
        kwargs["client_order_id"] = client_order_id
    return QuoteDecision(side=side, action=action, trigger=QuoteTrigger.INITIAL_QUOTE, **kwargs)


def maker_decision(bid_action: QuoteAction = QuoteAction.NONE, ask_action: QuoteAction = QuoteAction.NONE,
                   *, existing_id: str | None = None) -> MakerDecision:
    """构造真实 `MakerDecision`（bid=BUY / ask=SELL 由既有契约强制）。"""
    bid = quote(Side.BUY, bid_action, client_order_id=existing_id)
    ask = quote(Side.SELL, ask_action)
    return MakerDecision(symbol="BTCUSDT", at_ms=1, bid=bid, ask=ask,
                         bid_desired=bid_action is not QuoteAction.NONE,
                         ask_desired=ask_action is not QuoteAction.NONE)


class DecisionLoopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 真实 market_state_hash 需要真 MarketState；编排行为与哈希算法无关 ⇒ 固定化
        patcher = mock.patch.object(decision_loop_module, "_state_hash",
                                             lambda s: f"hash:{getattr(s, 'marker', '?')}")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.now = 1_000

    def loop(self, *, decision: Any, mode: RuntimeMode = RuntimeMode.PAPER, tradeable: bool = True,
             policy: Any = ..., engine: _Engine | None = None, tracker: _Tracker | None = None,
             prediction_runtime: Any = None, state_provider: Any = None) -> RuntimeDecisionLoop:
        return RuntimeDecisionLoop(
            config=DecisionLoopConfig(symbol="BTCUSDT", mode=mode, tick_ms=1,
                                      decision_interval_ms=5_000, prediction_min_interval_ms=1),
            state_provider=(state_provider if state_provider is not None
                            else (lambda: state(tradeable=tradeable))),
            engine=engine if engine is not None else _Engine(),
            tracker=tracker if tracker is not None else _Tracker(),
            risk_budget_provider=lambda snapshot: 100.0,
            clock=lambda: self.now,
            policy=(_Policy(decision) if policy is ... else policy),
            prediction_runtime=prediction_runtime)

    # ------------------------------------------------------------------ 契约

    def test_config_rejects_invalid_values(self) -> None:
        with self.assertRaises(DecisionLoopError):
            DecisionLoopConfig(symbol="", mode=RuntimeMode.PAPER)
        with self.assertRaises(DecisionLoopError):
            DecisionLoopConfig(symbol="BTCUSDT", mode="paper")          # type: ignore[arg-type]
        with self.assertRaises(DecisionLoopError):
            DecisionLoopConfig(symbol="BTCUSDT", mode=RuntimeMode.PAPER, tick_ms=0)

    def test_policy_without_decide_is_rejected(self) -> None:
        with self.assertRaises(DecisionLoopError):
            self.loop(decision=None, policy=object())

    def test_paper_does_not_require_live_readiness_and_testnet_does(self) -> None:
        self.assertFalse(self.loop(decision=None).config.readiness_required)
        self.assertTrue(RuntimeDecisionLoop(
            config=DecisionLoopConfig(symbol="BTCUSDT", mode=RuntimeMode.TESTNET),
            state_provider=lambda: None, engine=_Engine(), tracker=_Tracker(),
            risk_budget_provider=lambda snapshot: None, clock=lambda: self.now,
            policy=None).config.readiness_required)

    # ------------------------------------------------------------------ 决策 → 执行

    def test_place_is_submitted_through_the_engine(self) -> None:
        engine = _Engine()
        decision = maker_decision(QuoteAction.PLACE, QuoteAction.PLACE)
        loop = self.loop(decision=decision, engine=engine, prediction_runtime=_PredictionRuntime())

        loop.tick_once()

        self.assertEqual(len(engine.submitted), 2)
        self.assertEqual(loop.status.submits, 2)
        self.assertEqual(loop.status.decisions, 1)
        self.assertEqual(engine.polled, [self.now])                   # 每轮 poll 一次（真实回调）

    def test_keep_and_none_never_touch_the_engine(self) -> None:
        engine = _Engine()
        decision = maker_decision(QuoteAction.KEEP, QuoteAction.NONE, existing_id="o-1")
        loop = self.loop(decision=decision, engine=engine, prediction_runtime=_PredictionRuntime())

        loop.tick_once()

        self.assertEqual(engine.submitted, [])
        self.assertEqual(loop.status.submits, 0)

    def test_cancel_only_cancels(self) -> None:
        engine = _Engine()
        loop = self.loop(decision=maker_decision(QuoteAction.CANCEL, existing_id="o-1"),
                         engine=engine, prediction_runtime=_PredictionRuntime())

        loop.tick_once()

        self.assertEqual(engine.cancelled, ["o-1"])
        self.assertEqual(engine.submitted, [])
        self.assertEqual(loop.status.cancels, 1)

    def test_replace_is_cancel_before_replace_and_waits_for_terminal_state(self) -> None:
        engine, tracker = _Engine(), _Tracker(terminal_after_cancel=False)
        loop = self.loop(decision=maker_decision(QuoteAction.REPLACE, existing_id="o-1"),
                         engine=engine, tracker=tracker, prediction_runtime=_PredictionRuntime())

        loop.tick_once()
        self.assertEqual(engine.cancelled, ["o-1"])
        self.assertEqual(engine.submitted, [])                        # 撤单未确认前不下新单

        tracker._settle.add("o-1")                                    # 撤单 ack（终态）
        self.now += 6_000
        # 撤单确认后 policy 的输入（tracker.active()）不再含该单 ⇒ 本轮不再 REPLACE，
        # 待替换的挂单仍由 loop 的 pending-replace 队列补上（不丢单）
        loop.policy.decision = maker_decision()
        loop.tick_once()

        self.assertEqual(len(engine.submitted), 1)
        self.assertEqual(engine.submitted[0].price, 100.0)
        self.assertEqual(loop.status.cancels, 1)

    def test_risk_rejection_is_counted_not_hidden(self) -> None:
        engine = _Engine(reject=True)
        decision = maker_decision(QuoteAction.PLACE, QuoteAction.NONE)
        loop = self.loop(decision=decision, engine=engine, prediction_runtime=_PredictionRuntime())

        loop.tick_once()

        self.assertEqual(loop.status.risk_rejects, 1)
        self.assertEqual(loop.status.submits, 0)

    def test_readiness_gate_blocks_when_live_readiness_is_required(self) -> None:
        """TESTNET/LIVE 的 fail-closed 结构：readiness 未知 ⇒ 无 submit（本阶段不启用该写路径）。"""
        engine = _Engine()
        loop = RuntimeDecisionLoop(
            config=DecisionLoopConfig(symbol="BTCUSDT", mode=RuntimeMode.TESTNET),
            state_provider=lambda: state(), engine=engine, tracker=_Tracker(),
            risk_budget_provider=lambda snapshot: 100.0, clock=lambda: self.now,
            policy=_Policy(None), prediction_runtime=None)

        loop._submit(quote(Side.BUY, QuoteAction.PLACE), self.now, allowed=False)

        self.assertEqual(engine.submitted, [])
        self.assertEqual(loop.status.readiness_blocks, 1)

    # ------------------------------------------------------------------ 缺失事实的诚实行为

    def test_missing_policy_publishes_no_decision_and_does_not_invent_one(self) -> None:
        engine, published = _Engine(), []
        loop = self.loop(decision=None, policy=None, engine=engine, prediction_runtime=_PredictionRuntime())
        loop.on_decision = published.append

        loop.tick_once()

        self.assertIsNone(loop.latest_decision)
        self.assertEqual(published, [None])                           # 明确 ABSENT，而非伪造 HOLD
        self.assertEqual(loop.status.decisions, 0)
        self.assertEqual(engine.submitted, [])

    def test_missing_prediction_runtime_is_not_fabricated(self) -> None:
        loop = self.loop(decision=maker_decision(QuoteAction.NONE, QuoteAction.NONE))
        loop.tick_once()

        self.assertIsNone(loop.latest_prediction)
        self.assertEqual(loop.status.predictions_submitted, 0)

    def test_paper_readiness_is_recorded_but_not_a_submit_gate(self) -> None:
        engine = _Engine()
        decision = maker_decision(QuoteAction.PLACE, QuoteAction.NONE)
        loop = self.loop(decision=decision, engine=engine, prediction_runtime=_PredictionRuntime())

        loop.tick_once()

        fact = loop.readiness_result
        self.assertEqual(fact.status, "unavailable")
        self.assertIn(PAPER_READINESS_REASON, fact.reasons)
        self.assertFalse(fact.live_ready)
        self.assertEqual(len(engine.submitted), 1)                    # 人类裁决方案 A：不阻断 PAPER

    def test_untradeable_state_skips_decisions(self) -> None:
        engine = _Engine()
        loop = self.loop(decision=maker_decision(QuoteAction.PLACE, QuoteAction.NONE),
                         engine=engine, tradeable=False, prediction_runtime=_PredictionRuntime())

        loop.tick_once()

        self.assertEqual(loop.status.decisions, 0)
        self.assertEqual(engine.submitted, [])

    def test_no_market_state_yet_is_a_no_op(self) -> None:
        loop = self.loop(decision=None, state_provider=lambda: None)

        loop.tick_once()

        self.assertEqual(loop.status.decisions, 0)
        self.assertEqual(loop.status.predictions_submitted, 0)

    # ------------------------------------------------------------------ 预测

    def test_prediction_is_submitted_once_per_unchanged_market_state(self) -> None:
        runtime = _PredictionRuntime(record=SimpleNamespace(market_state_hash="hash:s1"))
        loop = self.loop(decision=maker_decision(QuoteAction.NONE, QuoteAction.NONE),
                         prediction_runtime=runtime)
        self.now += 10_000

        loop.tick_once()
        self.now += 10_000
        loop.tick_once()

        self.assertEqual(len(runtime.submitted), 1)                   # 状态未变 + 未过期 ⇒ 不重复调用 provider
        self.assertEqual(loop.status.predictions_accepted, 1)

    def test_prediction_is_resubmitted_when_state_changes_or_expires(self) -> None:
        current = {"marker": "s1"}
        runtime = _PredictionRuntime(record=SimpleNamespace(market_state_hash="hash:s1"))
        loop = self.loop(decision=maker_decision(QuoteAction.NONE, QuoteAction.NONE),
                         prediction_runtime=runtime,
                         state_provider=lambda: state(marker=current["marker"]))
        self.now += 10_000
        loop.tick_once()

        current["marker"] = "s2"                                      # 状态变化
        self.now += 10_000
        loop.tick_once()

        runtime.record = SimpleNamespace(market_state_hash="hash:s2")
        runtime._expired = True                                       # 过期
        self.now += 10_000
        loop.tick_once()

        self.assertEqual(len(runtime.submitted), 3)

    def test_provider_backoff_is_respected(self) -> None:
        runtime = _PredictionRuntime()
        runtime.next_retry_at = self.now + 60_000                      # scheduler 退避中
        loop = self.loop(decision=maker_decision(QuoteAction.NONE, QuoteAction.NONE),
                         prediction_runtime=runtime)
        self.now += 10_000

        loop.tick_once()

        self.assertEqual(runtime.submitted, [])

    def test_prediction_callback_receives_the_record(self) -> None:
        published: list[Any] = []
        record = SimpleNamespace(market_state_hash="hash:s1")
        loop = self.loop(decision=maker_decision(QuoteAction.NONE, QuoteAction.NONE),
                         prediction_runtime=_PredictionRuntime(record=record))
        loop.on_prediction = published.append

        loop.tick_once()

        self.assertEqual(published, [record])

    # ------------------------------------------------------------------ 节流与生命周期

    def test_decisions_are_throttled_by_state_hash_and_cadence(self) -> None:
        engine = _Engine()
        loop = self.loop(decision=maker_decision(QuoteAction.NONE, QuoteAction.NONE),
                         engine=engine, prediction_runtime=_PredictionRuntime())

        loop.tick_once()
        self.now += 1_000
        loop.tick_once()                                              # 状态未变且在 cadence 内

        self.assertEqual(loop.status.decisions, 1)

        self.now += 5_000                                             # 超过 decision_interval_ms
        loop.tick_once()
        self.assertEqual(loop.status.decisions, 2)

    def test_budget_and_kill_switch_come_from_injected_providers(self) -> None:
        policy = _Policy(maker_decision(QuoteAction.NONE, QuoteAction.NONE))
        loop = self.loop(decision=None, policy=policy, prediction_runtime=_PredictionRuntime())
        loop.kill_switch_provider = lambda: "HALT_ALL"

        loop.tick_once()

        self.assertEqual(policy.calls[0]["remaining_risk_budget"], 100.0)
        self.assertEqual(policy.calls[0]["kill_switch"], "HALT_ALL")
        self.assertTrue(policy.calls[0]["state"].quality.tradeable)

    def test_unknown_budget_is_passed_through_as_none(self) -> None:
        policy = _Policy(maker_decision(QuoteAction.NONE, QuoteAction.NONE))
        loop = self.loop(decision=None, policy=policy, prediction_runtime=_PredictionRuntime())
        loop.risk_budget_provider = lambda snapshot: None

        loop.tick_once()

        self.assertIsNone(policy.calls[0]["remaining_risk_budget"])

    def test_start_and_stop_leave_no_thread_behind(self) -> None:
        loop = self.loop(decision=maker_decision(QuoteAction.NONE, QuoteAction.NONE),
                         prediction_runtime=_PredictionRuntime())
        loop.start()
        self.assertTrue(loop.running)
        with self.assertRaises(DecisionLoopError):
            loop.start()                                              # 不可重复启动

        loop.stop()

        self.assertFalse(loop.running)
        self.assertEqual([t for t in threading.enumerate() if "probex-decision" in t.name], [])
        loop.stop()                                                   # 幂等

    def test_tick_failure_is_recorded_and_does_not_kill_the_loop(self) -> None:
        loop = self.loop(decision=maker_decision(QuoteAction.NONE, QuoteAction.NONE))

        def explode() -> Any:
            raise RuntimeError("feed down")

        loop.state_provider = explode
        loop.start()
        deadline = 2.0
        import time

        started = time.time()
        while time.time() - started < deadline and loop.status.errors == 0:
            time.sleep(0.01)
        loop.stop()

        self.assertGreater(loop.status.errors, 0)
        self.assertEqual(loop.status.last_error, "RuntimeError")


if __name__ == "__main__":
    unittest.main()
