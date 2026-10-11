"""P0017.2 Batch 4：经真实 ExecutionEngine 的组合第二道闸端到端（离线）。

证明：两个 symbol 各自满足单标的限额，但合计超组合限额时被 PORTFOLIO_NOTIONAL_LIMIT 拒绝；
候选单在 tracker 登记前被拦（不重复消费预算）；reduce-only 不被组合闸误拦。
"""

from __future__ import annotations

import unittest

from execution.adapters.paper import PaperBroker
from execution.engine import ExecutionEngine
from execution.manager import OrderManager
from execution.tracker import OrderTracker
from portfolio.accounting import AccountingCore
from portfolio.types import Side
from risk.limits import RiskLimits
from risk.portfolio_budget import PortfolioCommitment
from risk.types import KillSwitchMode, OrderProposal


class PortfolioGateEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.accounting = AccountingCore(initial_balance=100_000.0)
        self.tracker = OrderTracker(session_id="p00172")
        self.broker = PaperBroker()
        self.manager = OrderManager(tracker=self.tracker, adapter=self.broker)
        # 每个 symbol 持仓名义上限 8000（各自都过），组合上限 10000
        self.limits = RiskLimits(
            max_position_notional=8_000.0,
            max_portfolio_gross_notional=10_000.0,
            kill_switch_mode=KillSwitchMode.NORMAL)

        def commitment_provider():
            positions = {}
            net_qty = {}
            unpriced = set()
            for pos in self.accounting.positions:
                net_qty[pos.symbol] = pos.qty
                if pos.qty != 0.0 and pos.notional is None:
                    unpriced.add(pos.symbol)      # L1：非 flat 缺 mark ⇒ 不可量化
                elif pos.notional:
                    positions[pos.symbol] = float(pos.notional)
            pending = self.tracker.exposure_by_symbol()
            unresolved = {s: (s in unpriced) for s in set(positions) | set(pending) | unpriced}
            return PortfolioCommitment(
                position_by_symbol=positions,
                pending_by_symbol={s: v["total"] for s, v in pending.items()},
                has_unresolved_by_symbol=unresolved), net_qty

        self.engine = ExecutionEngine(
            accounting=self.accounting, gate=__import__("risk.gate", fromlist=["RiskGate"]).RiskGate(self.limits),
            manager=self.manager, portfolio_commitment_provider=commitment_provider)
        # 标记价格（两 symbol）
        self.accounting.update_mark_price("BTCUSDT", 60_000.0, timestamp=1)
        self.accounting.update_mark_price("ETHUSDT", 3_000.0, timestamp=1)

    def _buy(self, symbol: str, qty: float, price: float, *, reduce_only: bool = False):
        return OrderProposal(symbol=symbol, side=Side.BUY, quantity=qty, price=price,
                               reduce_only=reduce_only)

    def _fill_active(self, result, *, price: float, now_ms: int):
        """通过 paper broker 对刚提交的订单全额成交并记账。"""
        order_id = result.order.client_order_id
        self.broker.fill(order_id, quantity=result.order.quantity, price=price,
                          timestamp=now_ms)
        polled = self.engine.poll(now_ms=now_ms + 1)
        return polled

    def test_second_symbol_blocked_when_portfolio_exceeds(self) -> None:
        # 第一笔：BTC 0.1 @60000 = 6000（< 8000 单标的；组合 6000 < 10000）⇒ 允许并成交
        r1 = self.engine.submit(self._buy("BTCUSDT", 0.1, 60_000.0), now_ms=10)
        self.assertTrue(r1.submitted, r1.decision.details if r1.rejected else "")
        self._fill_active(r1, price=60_000.0, now_ms=11)
        self.assertAlmostEqual(self.accounting.position("BTCUSDT").notional, 6_000.0, places=3)
        self.assertEqual(len(self.tracker.active()), 0)  # 已 FILLED，不占在途

        # 第二笔：ETH 2 @3000 = 6000（< 8000 单标的），但组合 12000 > 10000 ⇒ 组合闸拒绝
        r2 = self.engine.submit(self._buy("ETHUSDT", 2.0, 3_000.0), now_ms=12)
        self.assertTrue(r2.rejected)
        self.assertEqual(r2.decision.reason_code.value, "PORTFOLIO_NOTIONAL_LIMIT")
        # 候选单被拦在登记前 ⇒ 不产生 ETH 订单
        self.assertNotIn("ETHUSDT", {o.symbol for o in self.tracker.orders})

    def test_within_portfolio_limit_passes_both_gates(self) -> None:
        r1 = self.engine.submit(self._buy("BTCUSDT", 0.05, 60_000.0), now_ms=10)  # 3000
        self.assertTrue(r1.submitted)
        self._fill_active(r1, price=60_000.0, now_ms=11)
        r2 = self.engine.submit(self._buy("ETHUSDT", 1.0, 3_000.0), now_ms=12)  # 3000；合计 6000
        self.assertTrue(r2.submitted, r2.decision.details if r2.rejected else "")

    def test_pending_order_consumes_portfolio_budget_before_fill(self) -> None:
        # BTC 在途挂单 6000（未成交）⇒ 已占组合预算；ETH 再 6000 应被拦
        r1 = self.engine.submit(self._buy("BTCUSDT", 0.1, 60_000.0), now_ms=10)
        self.assertTrue(r1.submitted)
        self.assertEqual(len(self.tracker.active()), 1)
        r2 = self.engine.submit(self._buy("ETHUSDT", 2.0, 3_000.0), now_ms=11)
        self.assertTrue(r2.rejected)
        self.assertEqual(r2.decision.reason_code.value, "PORTFOLIO_NOTIONAL_LIMIT")

    def test_portfolio_disabled_when_limit_none(self) -> None:
        engine = ExecutionEngine(
            accounting=self.accounting,
            gate=__import__("risk.gate", fromlist=["RiskGate"]).RiskGate(
                RiskLimits(max_position_notional=1_000_000.0,
                            kill_switch_mode=KillSwitchMode.NORMAL)),
            manager=self.manager,
            portfolio_commitment_provider=lambda: (
                PortfolioCommitment({}, {}, {}), {}))
        result = engine.submit(self._buy("ETHUSDT", 2.0, 3_000.0), now_ms=5)
        self.assertTrue(result.submitted)

    # ------------------------------------------------------------------
    # L1 修正：非 flat 持仓缺 mark 时组合闸必须 fail-closed（不能静默跳过）
    # ------------------------------------------------------------------
    def _fresh_engine(self):
        """L1 测试需要可重复 bootstrap 的独立账本/引擎。"""
        accounting = AccountingCore(initial_balance=100_000.0)
        tracker = OrderTracker(session_id="p00172-l1")
        manager = OrderManager(tracker=tracker, adapter=PaperBroker())

        def provider():
            pos_map, net_qty, unpriced = {}, {}, set()
            for pos in accounting.positions:
                net_qty[pos.symbol] = pos.qty
                if pos.qty != 0.0 and pos.notional is None:
                    unpriced.add(pos.symbol)
                elif pos.notional:
                    pos_map[pos.symbol] = float(pos.notional)
            pending = tracker.exposure_by_symbol()
            unresolved = {s: s in unpriced for s in set(pos_map) | set(pending) | unpriced}
            return PortfolioCommitment(pos_map,
                                         {s: v["total"] for s, v in pending.items()},
                                         unresolved), net_qty

        from risk.gate import RiskGate
        return accounting, tracker, manager, ExecutionEngine(
            accounting=accounting, gate=RiskGate(self.limits), manager=manager,
            portfolio_commitment_provider=provider)

    def _fill(self, engine, manager, tracker, symbol: str, qty: float, price: float,
                *, now_ms: int, mark: float | None):
        """提交并全额成交（mark=None：建立了持仓但不设 mark，模拟缺有效标记价格）。"""
        submitted = engine.submit(self._buy(symbol, qty, price), now_ms=now_ms)
        assert submitted.submitted, submitted.decision.details if submitted.rejected else "submitted"
        manager.adapter.fill(submitted.order.client_order_id, quantity=qty, price=price,
                                timestamp=now_ms + 1)
        engine.poll(now_ms=now_ms + 2)
        if mark is not None:
            engine.accounting.update_mark_price(symbol, mark, timestamp=now_ms + 3)
        return submitted

    def _record_unpriced_fill(self, accounting: AccountingCore, symbol: str, side: Side,
                                qty: float, price: float):
        """直接用 canonical Fill 建立一个成交后缺 mark 的持仓（FillLedger 双键去重）。"""
        from market.events.types import Venue
        from portfolio.types import Fill
        fill = Fill(
            fill_id=f"f-{symbol}", order_id=f"o-{symbol}", venue=Venue.BINANCE,
            symbol=symbol, side=side, price=price, quantity=qty, fee=0.0, fee_asset="USDT",
            trade_id=f"t-{symbol}", exchange_ts=1, receive_ts=1)
        accounting.record_fill(fill)

    def test_missing_mark_on_one_holding_blocks_new_order_on_other_symbol(self) -> None:
        accounting, tracker, manager, engine = self._fresh_engine()
        # A：成交后从未设 mark ⇒ 持仓名义不可估（真实的 markless holding）
        self._record_unpriced_fill(accounting, "BTCUSDT", Side.BUY, 0.1, 60_000.0)
        self.assertIsNone(accounting.position("BTCUSDT").notional)
        # B：同样用 canonical Fill 建立有 mark 的 2000 名义持仓（不经过 submit，避免被 A 的不可估状态拦）
        self._record_unpriced_fill(accounting, "ETHUSDT", Side.BUY, 2 / 3, 3_000.0)
        accounting.update_mark_price("ETHUSDT", 3_000.0, timestamp=2)
        # 买 C 3000：A 缺 mark ⇒ 组合不可估 ⇒ 必须拒绝（不得跳过 A 而放行）
        result = engine.submit(self._buy("SOLUSDT", 30.0, 100.0), now_ms=20)
        self.assertTrue(result.rejected)
        self.assertEqual(result.decision.reason_code.value, "PORTFOLIO_EXPOSURE_UNKNOWN")
        self.assertNotIn("SOLUSDT", {o.symbol for o in tracker.orders})

    def test_portfolio_re_evaluates_after_mark_recovers(self) -> None:
        accounting, tracker, manager, engine = self._fresh_engine()
        self._record_unpriced_fill(accounting, "BTCUSDT", Side.BUY, 0.05, 60_000.0)
        accounting.update_mark_price("ETHUSDT", 3_000.0, timestamp=19)  # 候选标的自身需有 mark
        blocked = engine.submit(self._buy("ETHUSDT", 1.0, 3_000.0), now_ms=20)
        self.assertTrue(blocked.rejected)
        self.assertEqual(blocked.decision.reason_code.value, "PORTFOLIO_EXPOSURE_UNKNOWN")
        accounting.update_mark_price("BTCUSDT", 60_000.0, timestamp=21)
        allowed = engine.submit(self._buy("ETHUSDT", 1.0, 3_000.0), now_ms=22)
        self.assertTrue(allowed.submitted, allowed.decision.details if allowed.rejected else "")

    def test_flat_position_without_mark_does_not_block(self) -> None:
        # 无持仓 + 有有效 mark：flat 不构成不可估持仓，正常走组合闸
        accounting, tracker, manager, engine = self._fresh_engine()
        accounting.update_mark_price("ETHUSDT", 3_000.0, timestamp=1)
        result = engine.submit(self._buy("ETHUSDT", 1.0, 3_000.0), now_ms=20)
        self.assertTrue(result.submitted, result.decision.details if result.rejected else "")

    def test_unpriced_symbol_with_no_position_is_not_flagged(self) -> None:
        # 未交易过的 symbol 既无持仓也无 mark，不应被当成不可估持仓（只看实际持仓）
        accounting, tracker, manager, engine = self._fresh_engine()
        accounting.update_mark_price("ETHUSDT", 3_000.0, timestamp=1)
        result = engine.submit(self._buy("ETHUSDT", 1.0, 3_000.0), now_ms=20)
        self.assertTrue(result.submitted)


if __name__ == "__main__":
    unittest.main()
