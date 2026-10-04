"""P0001.16 §14 B / 人类裁决「选择 B」：TESTNET acceptance capability 的**结构性隔离**验收。

证明：

- 默认**不存在**（flag 关闭 ⇒ `None`）；
- TESTNET only / BTCUSDT only / notional ≤ 100 USDT；
- 产品默认路径（`MakerPolicy`）仍然是 `LIMIT + GTX + post-only`，**不能**自然发 taker 单；
- adapter 在没有许可时对非 post-only 单一律本地拒绝（`post_only_required`）；
- 有许可时只走 **IOC**（永不挂单），且仍经 authority/risk/connector 主链；
- PAPER 不受影响；Strategy / Product / UI / Assistant / CAPITAL Action 无法触发（architecture guard 另测）。
"""

from __future__ import annotations

import pathlib
import unittest

from execution.acceptance import (ACCEPTANCE_FLAG_ENV, ACCEPTANCE_SYMBOL, DEFAULT_MAX_NOTIONAL, AcceptanceError,
                                  TestnetAcceptancePermission, testnet_acceptance_permission)
from execution.types import InvalidOrderError, Order, OrderStatus
from market.events.types import Venue
from portfolio.types import Side

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


def order(*, post_only: bool = False, symbol: str = ACCEPTANCE_SYMBOL, price: float = 60_000.0,
          quantity: float = 0.001) -> Order:
    return Order(client_order_id="probex-testnet-000001", venue=Venue.BINANCE, symbol=symbol,
                 side=Side.BUY, price=price, quantity=quantity, status=OrderStatus.PENDING_CREATE,
                 created_at=1, updated_at=1, post_only=post_only, reduce_only=False)


class AcceptancePermissionTest(unittest.TestCase):
    def test_default_is_disabled(self) -> None:
        permission = testnet_acceptance_permission(environment="testnet", symbol=ACCEPTANCE_SYMBOL, enabled=False)
        self.assertIsNone(permission)

    def test_flag_off_by_default_in_this_environment(self) -> None:
        import os

        self.assertNotIn(ACCEPTANCE_FLAG_ENV, os.environ)
        self.assertIsNone(testnet_acceptance_permission(environment="testnet", symbol=ACCEPTANCE_SYMBOL))

    def test_mainnet_and_paper_have_no_capability(self) -> None:
        for environment in ("mainnet", "paper", ""):
            with self.subTest(environment=environment):
                self.assertIsNone(testnet_acceptance_permission(
                    environment=environment, symbol=ACCEPTANCE_SYMBOL, enabled=True))

    def test_only_btcusdt_is_permitted(self) -> None:
        self.assertIsNone(testnet_acceptance_permission(environment="testnet", symbol="ETHUSDT", enabled=True))
        with self.assertRaises(AcceptanceError):
            TestnetAcceptancePermission(symbol="ETHUSDT", max_notional=50.0, granted_by="test")

    def test_notional_cap_is_capped_at_one_hundred(self) -> None:
        permission = testnet_acceptance_permission(environment="testnet", symbol=ACCEPTANCE_SYMBOL,
                                                  enabled=True, max_notional=10_000.0)
        assert permission is not None
        self.assertEqual(permission.max_notional, DEFAULT_MAX_NOTIONAL)
        with self.assertRaises(AcceptanceError):
            TestnetAcceptancePermission(symbol=ACCEPTANCE_SYMBOL, max_notional=200.0, granted_by="test")

    def test_granted_by_is_recorded_for_audit(self) -> None:
        permission = testnet_acceptance_permission(environment="testnet", symbol=ACCEPTANCE_SYMBOL,
                                                  enabled=True, granted_by="human-session-2026-10-04")
        assert permission is not None
        self.assertEqual(permission.view()["granted_by"], "human-session-2026-10-04")
        self.assertEqual(permission.view()["time_in_force"], "IOC")
        self.assertFalse(permission.view()["post_only"])

    def test_authorize_checks_symbol_and_notional_per_order(self) -> None:
        permission = TestnetAcceptancePermission(symbol=ACCEPTANCE_SYMBOL, max_notional=100.0,
                                                 granted_by="test")

        self.assertEqual(permission.authorize(order=order())[0], True)
        self.assertEqual(permission.authorize(order=order(post_only=True))[0], True)   # 无需许可
        allowed, detail = permission.authorize(order=order(symbol="ETHUSDT"))
        self.assertFalse(allowed)
        self.assertIn("symbol", detail)
        allowed, detail = permission.authorize(order=order(price=60_000.0, quantity=0.01))   # 600 USDT
        self.assertFalse(allowed)
        self.assertIn("notional", detail)


class AdapterGateTest(unittest.TestCase):
    """adapter 层：没有许可 ⇒ 结构性拒绝；有许可 ⇒ 只走 IOC，且 post-only 仍走 GTX。"""

    def stack(self, *, acceptance: TestnetAcceptancePermission | None = None):
        from connectors.binance.execution.adapter import BinanceExecutionAdapter
        from readiness.types import Environment
        from tests.execution_support_live import FakeExecutionFetcher, echoing_ack, client
        from tests.unit.test_execution_adapter import context as authority_context
        from tests.unit.test_execution_adapter import rules as rules_provider

        fetcher = FakeExecutionFetcher(responses={"POST": echoing_ack})
        adapter = BinanceExecutionAdapter(
            rest=client(fetcher), environment=Environment.TESTNET,
            authority_provider=authority_context, rules_provider=rules_provider,
            symbol=ACCEPTANCE_SYMBOL, acceptance=acceptance)
        return adapter, fetcher

    def test_without_permission_taker_is_refused_but_post_only_works(self) -> None:
        adapter, fetcher = self.stack()
        self.assertIsNone(adapter.acceptance_view)          # 能力结构性不存在

        refused = adapter.submit_with_outcome(order(post_only=False))
        self.assertEqual(refused.classification.value, "CONFIRMED_REJECTED")
        self.assertEqual(refused.rejection_message, "post_only_required")
        self.assertEqual(fetcher.calls, [])                 # 未发出任何写请求

        accepted = adapter.submit_with_outcome(order(post_only=True))
        self.assertEqual(accepted.classification.value, "CONFIRMED_ACCEPTED")
        self.assertIn("timeInForce=GTX", fetcher.seen_queries[-1])

    def test_with_permission_taker_uses_ioc_and_post_only_still_uses_gtx(self) -> None:
        permission = TestnetAcceptancePermission(symbol=ACCEPTANCE_SYMBOL, max_notional=100.0,
                                                 granted_by="human-session-2026-10-04")
        adapter, fetcher = self.stack(acceptance=permission)

        self.assertEqual(adapter.acceptance_view["time_in_force"], "IOC")
        accepted = adapter.submit_with_outcome(order(post_only=False))
        self.assertEqual(accepted.classification.value, "CONFIRMED_ACCEPTED")
        self.assertIn("timeInForce=IOC", fetcher.seen_queries[-1])

        adapter.submit_with_outcome(order(post_only=True))
        self.assertIn("timeInForce=GTX", fetcher.seen_queries[-1])   # 正常语义不变

    def test_permission_does_not_bypass_symbol_or_notional_cap(self) -> None:
        permission = TestnetAcceptancePermission(symbol=ACCEPTANCE_SYMBOL, max_notional=100.0,
                                                 granted_by="test")
        adapter, fetcher = self.stack(acceptance=permission)

        refused = adapter.submit_with_outcome(order(post_only=False, price=60_000.0, quantity=0.01))
        self.assertEqual(refused.classification.value, "CONFIRMED_REJECTED")
        self.assertIn("notional", refused.detail)           # 600 USDT > 100 USDT 上限
        self.assertEqual(fetcher.calls, [])


class StrategyIsGtxOnlyTest(unittest.TestCase):
    def test_maker_policy_never_emits_taker_proposals(self) -> None:
        """人类裁决第 2 条：Maker/生产策略路径永远是 post-only。"""
        for path in sorted((PROJECT_ROOT / "strategy").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("post_only=False", source)

    def test_acceptance_contract_is_not_reachable_from_strategy_or_product_surfaces(self) -> None:
        """人类裁决第 3 条：Strategy / Product / UI / Assistant / Action 都不能触发验收能力。"""
        guarded = ["strategy", "product", "assistant", "actions", "ui"]
        for root in guarded:
            for path in sorted((PROJECT_ROOT / root).rglob("*")):
                if path.suffix not in (".py", ".js") or "__pycache__" in str(path):
                    continue
                source = path.read_text(encoding="utf-8", errors="ignore")
                with self.subTest(module=f"{root}/{path.name}"):
                    self.assertNotIn("TestnetAcceptancePermission", source)
                    self.assertNotIn("testnet_acceptance_permission", source)
                    self.assertNotIn("submit_ioc_limit", source)
                    self.assertNotIn("ACCEPTANCE_FLAG_ENV", source)

    def test_paper_path_is_unaffected(self) -> None:
        from connectors.paper import PaperExecutionConnector

        paper_source = (PROJECT_ROOT / "connectors" / "paper" / "execution.py").read_text(encoding="utf-8")
        self.assertNotIn("acceptance", paper_source)
        self.assertTrue(hasattr(PaperExecutionConnector, "submit"))


if __name__ == "__main__":
    unittest.main()
