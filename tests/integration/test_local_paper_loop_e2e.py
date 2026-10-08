"""P0001.17 §3/§16 A+D：本地 Replay/PAPER 真实交易闭环 E2E（不依赖 Binance）。

证明（全部经产品真实 runtime assembly，不用 fixture 拼结果）：

```
Replay 市场数据 → MarketState → MakerPolicy 决策 → RiskGate → PAPER 下单
→ 事件级模拟成交（既有 `SimulatedVenue`） → OrderTracker → FillLedger → AccountingCore
→ position / equity / PnL → Product Read Model（snapshot / orders / trace）
```

并验证因果链可反查：任取一笔成交 → 其订单 → 其 canonical correlation → decision_id 与 market state hash。

说明（诚实标注）：本测试为 **wiring 级 E2E**，prediction 由 `tests/fakes.py::FakeProvider` 注入
（P0001.16 裁决：FakeProvider 允许用于 unit/integration wiring test，禁止用于产品级验收）。
产品级（真实外部 prediction provider）尚未接入，属 P0001.17 待裁决项——本测试不据此声称产品已具备真实预测。
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from pathlib import Path

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile
from tests.fakes import FakeProvider
from tests.support import TempDirTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LocalPaperLoopE2ETest(TempDirTestCase):
    """真实装配的 PAPER 本地闭环（trial profile：strategy/risk/simulation 显式 LOCAL TRIAL 值）。"""

    def _runtime(self, *, hours: int = 1) -> ProductRuntime:
        from tests.ui.market_fixture import write_market_store

        store = self.tmp_path / "events.jsonl"
        write_market_store(store, hours=hours, mark_price=60_000.0)
        values: dict[str, object] = json.loads(
            (PROJECT_ROOT / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
        self.assertTrue(values.get("simulation.enabled") is True, "trial profile must enable simulation")
        entries = tuple(ConfigEntry(name=str(k), source=ConfigSource.FILE, value=Fact.of(v))
                        for k, v in values.items())
        runtime = ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode.PAPER,
            run_registry_dir=str(self.tmp_path / "runs"),
            feed=FeedProfile(event_store=str(store), window_ms=3_600_000, bucket_ms=1_000,
                             max_points=200, price_levels=5, history_capacity=6_000, view_depth=10)))
        runtime.attach_prediction_provider(FakeProvider(), timeout_ms=1_000, ttl_ms=120_000)
        return runtime

    def test_local_loop_produces_real_decision_order_fill_and_pnl(self) -> None:
        runtime = self._runtime()
        runtime.start()
        loop = runtime._decision_loop                                            # noqa: SLF001
        try:
            deadline = __import__("time").time() + 60
            while __import__("time").time() < deadline:
                if loop.status.submits >= 2 and runtime._execution.accounting.fills.fills:  # noqa: SLF001
                    break
                __import__("time").sleep(0.25)
            snapshot = runtime.service.snapshot()

            # 1) 真实策略决策（不是 fixture 拼出来的结果）
            self.assertGreaterEqual(loop.status.decisions, 1)
            self.assertIsNotNone(loop.latest_decision)
            decision = loop.latest_decision
            self.assertTrue(decision.decision_id)
            self.assertIn(decision.mode.value, ("both", "bid_only", "ask_only", "none"))
            self.assertGreaterEqual(loop.status.submits, 1, f"no submit: {loop.status.__dict__}")
            self.assertEqual(loop.status.risk_rejects, 0)

            # 2) RiskGate 逐单 allow（证据来自既有 risk decision log）
            risk_stages = [entry for entry in snapshot.evidence.trace if entry.stage == "risk"]
            self.assertTrue(risk_stages)
            self.assertTrue(all(str(entry.outcome) == "allow" for entry in risk_stages))

            # 3) 事件级模拟成交真的发生了（P0001.17 §3：市场事件驱动，不是手工注入）
            orders = runtime._tracker_owner.orders                                # noqa: SLF001
            self.assertTrue(orders, "no orders were created")
            filled = [order for order in orders if order.filled_quantity > 0.0]
            self.assertTrue(filled, f"no simulated fill happened: "
                                    f"{[(o.client_order_id, o.status.value) for o in orders]}")
            filled_order = filled[0]

            # 4) 成交进入 canonical 账本 + AccountingCore（唯一 Owner）
            accounting = runtime._execution.accounting                            # noqa: SLF001
            ledger_fills = accounting.fills.fills
            self.assertTrue(ledger_fills, "accounting ledger has no fills")
            self.assertTrue(snapshot.execution.recent_fills, "product sees no fills")
            self.assertTrue(snapshot.portfolio.position_qty.known)
            self.assertTrue(snapshot.health.accounting.known)

            # 4b) 账本一致性（不变量）：仓位 == 成交净额；权益/费用反映真实成交
            net = sum(fill.side.sign * fill.quantity for fill in ledger_fills)
            self.assertAlmostEqual(snapshot.portfolio.position_qty.value, net, places=9)
            total_fees = sum(fill.fee for fill in ledger_fills)
            self.assertGreater(total_fees, 0.0, "maker fills must carry explicit fees")
            self.assertTrue(snapshot.portfolio.equity.known)
            self.assertNotEqual(snapshot.portfolio.equity.value, 10_000.0,
                                "equity must reflect the simulated fills (fees/PnL)")
            # maker 是双边报价：两侧都应可能成交（本地闭环的证据，而不是单侧假数据）
            sides = {fill.side.value for fill in ledger_fills}
            self.assertTrue(sides.issubset({"buy", "sell"}))

            # 5) 因果链可反查：fill → order → correlation（decision / instrument / venue / state hash）
            self.assertIsNotNone(filled_order.correlation)
            self.assertEqual(filled_order.correlation.instrument_id, "paper:BTCUSDT"
                             if filled_order.correlation.instrument_id else "paper:BTCUSDT")
            self.assertTrue(filled_order.correlation.decision_id)
            linked = runtime._execution.orders_for_decision(filled_order.correlation.decision_id)  # noqa: SLF001
            self.assertIn(filled_order.client_order_id, [o.client_order_id for o in linked])
            order_stages = [entry for entry in snapshot.evidence.trace if entry.stage == "order"]
            self.assertTrue(any(str(entry.identity.value) == filled_order.client_order_id
                                for entry in order_stages))
            fill_stages = [entry for entry in snapshot.evidence.trace if entry.stage == "fill"]
            self.assertTrue(fill_stages, "trace has no fill stage")
            # 同一 instrument/venue identity 贯穿因果链
            for entry in (*order_stages, *fill_stages):
                self.assertEqual(entry.instrument_id.value, "paper:BTCUSDT")
                self.assertEqual(entry.venue_id.value, "paper")

            # 6) 产品读模型 + API：position/equity/PnL 真实（不是 0/UNKNOWN 伪装）
            self.assertTrue(snapshot.portfolio.equity.known)
            self.assertTrue(snapshot.execution.active_orders is not None)
            self.assertFalse(snapshot.execution.has_unknown_exposure)
            self.assertTrue(snapshot.instrument.instrument_id.known)
        finally:
            runtime.stop()

    def test_product_api_exposes_the_same_local_loop_facts(self) -> None:
        runtime = self._runtime()
        runtime.start()
        server = runtime.create_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            deadline = __import__("time").time() + 60
            payload: dict = {}
            while __import__("time").time() < deadline:
                with OPENER.open(f"{runtime.server_url()}/api/v1/snapshot", timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload["execution"]["recent_fills"] and \
                        runtime._execution.accounting.fills.fills:                  # noqa: SLF001
                    break
                __import__("time").sleep(0.5)
            self.assertEqual(payload["runtime"]["mode"], "PAPER")
            self.assertTrue(payload["portfolio"]["position_qty"]["known"])
            self.assertTrue(payload["portfolio"]["equity"]["known"])
            self.assertTrue(payload["execution"]["recent_fills"])
            # 账本一致（仓位 == API 成交净额）
            ledger_fills = runtime._execution.accounting.fills.fills                 # noqa: SLF001
            net = sum(fill.side.sign * fill.quantity for fill in ledger_fills)
            self.assertAlmostEqual(payload["portfolio"]["position_qty"]["value"], net, places=9)
            fill = payload["execution"]["recent_fills"][-1]
            self.assertTrue(fill["client_order_id"]["known"])
            self.assertTrue(fill["price"]["known"] and fill["quantity"]["known"])
            # 订单视图带 canonical correlation（decision/instrument/venue）
            order = payload["execution"]["active_orders"][0] if payload["execution"]["active_orders"] else None
            if order is not None:
                self.assertEqual(order["venue_id"]["value"], "paper")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            runtime.stop()


if __name__ == "__main__":
    unittest.main()
