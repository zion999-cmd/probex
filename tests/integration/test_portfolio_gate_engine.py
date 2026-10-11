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
            positions = {s: p.notional for s, p in
                          ((s, self.accounting.position(s)) for s in ("BTCUSDT", "ETHUSDT"))
                          if p.notional}
            pending = self.tracker.exposure_by_symbol()
            return PortfolioCommitment(
                position_by_symbol={k: float(v) for k, v in positions.items()},
                pending_by_symbol={s: v["total"] for s, v in pending.items()},
                has_unresolved_by_symbol={s: (c > 0) for s, c in
                                           ((s, sum(1 for o in self.tracker.unresolved_orders()
                                                     if o.symbol is None or o.symbol == s))
                                            for s in ("BTCUSDT", "ETHUSDT"))}), \
                {s: self.accounting.position(s).qty for s in ("BTCUSDT", "ETHUSDT")}

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


if __name__ == "__main__":
    unittest.main()
