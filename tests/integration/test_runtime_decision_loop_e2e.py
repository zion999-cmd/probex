"""P0001.14：真实装配下的决策闭环（Market → Prediction → MakerPolicy → Risk → PAPER Execution）。

约束（人类裁决 2026-10-03）：
- PAPER 的 live readiness **记录但不作为 submit authority**（方案 A）；
- risk budget 只由 Risk domain 提供，runtime/assembly 不做风险数学；
- prediction provider 不可用时如实 UNAVAILABLE（不伪造 50% / 不强制 BUY-SELL）；
- 只有一个 PaperBroker（`ExecutionEngine.manager.adapter`）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from connectors.paper import PaperExecutionConnector
from execution.adapters.paper import PaperBroker
from execution.simulation.venue import SimulatedVenue
from product import reason_catalog
from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile
from runtime.decision_loop import PAPER_READINESS_REASON
from strategy.maker.types import QuoteAction

#: 测试用 MakerPolicy 参数（**测试值**；真实值必须由 operator/profile 显式提供）
MAKER_VALUES: dict[str, float] = {
    "strategy.maker.tick_size": 0.1,
    "strategy.maker.quantity_step": 0.0001,
    "strategy.maker.min_quote_size": 0.0001,
    "strategy.maker.base_size": 0.001,
    "strategy.maker.max_back_ticks": 5,
    "strategy.maker.minimum_edge_bps": 0.0,
    "strategy.maker.prediction_horizon_ms": 15_000,
    "strategy.maker.adverse_selection_retreat": 0.6,
    "strategy.maker.adverse_selection_block": 0.99,
    "strategy.maker.imbalance_retreat": 0.5,
    "strategy.maker.microprice_skew_retreat_bps": 5.0,
    "strategy.maker.predicted_move_retreat": 0.5,
    "strategy.maker.target_position": 0.0,
    "strategy.maker.inventory_scale": 0.01,
    "strategy.maker.inventory_size_strength": 0.5,
    "strategy.maker.inventory_retreat_ticks_max": 3,
    "strategy.maker.size_factor_min": 0.25,
    "strategy.maker.size_factor_max": 1.0,
    "strategy.maker.confidence_ref_low": 0.2,
    "strategy.maker.confidence_ref_high": 0.8,
    "strategy.maker.confidence_factor_min": 0.25,
    "strategy.maker.risk_factor_min": 0.25,
    "strategy.maker.price_move_ticks_replace": 1,
    "strategy.maker.max_quote_age_ms": 30_000,
    "strategy.maker.size_drift_tolerance": 0.1,
}
RISK_VALUES: dict[str, float] = {"risk.max_position_notional": 100.0, "risk.max_position_qty": 0.01}
MARK_PRICE = 61_094.5


def wait_for(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class DecisionLoopE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probex-p0001.14-"))
        self.started_runtimes: list[ProductRuntime] = []

    def tearDown(self) -> None:
        # 必须在删除 run registry 目录**之前**停掉 runtime（否则 finish 读不到 index）
        for runtime in self.started_runtimes:
            if runtime._session.record.status.value == "RUNNING":       # noqa: SLF001
                runtime.stop()
        self.started_runtimes.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers

    def runtime(self, *, mode: RuntimeMode = RuntimeMode.PAPER, provider: bool = True,
                maker: bool = True, limits: bool = True) -> ProductRuntime:
        from tests.fakes import FakeProvider
        from tests.ui.market_fixture import write_market_store

        store = self.tmp / "events.jsonl"
        write_market_store(store, hours=3)
        values: dict[str, object] = json.loads(
            (Path(__file__).resolve().parents[2] / "profiles" / "trial-local.json").read_text())
        if not maker:                     # 显式移除（trial profile 可能自带 strategy.maker.*）
            values = {k: v for k, v in values.items() if not k.startswith("strategy.maker.")}
        if not limits:
            values = {k: v for k, v in values.items() if not k.startswith("risk.")}
        if not provider:                  # 显式移除 prediction 配置（trial profile 配了 local_trial）
            values = {k: v for k, v in values.items() if not k.startswith("prediction.")}
        for key, options in ((MAKER_VALUES, maker), (RISK_VALUES, limits)):
            if options:
                values.update(key)
        entries = tuple(ConfigEntry(name=name, source=ConfigSource.FILE, value=Fact.of(value))
                        for name, value in values.items())
        runtime = ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=entries, mode=mode,
            run_registry_dir=str(self.tmp / f"runs-{mode.value}"),
            feed=FeedProfile(event_store=str(store), window_ms=3_600_000, bucket_ms=1_000,
                             max_points=200, price_levels=5, history_capacity=6_000, view_depth=10)))
        if provider:
            runtime.attach_prediction_provider(FakeProvider(), timeout_ms=1_000, ttl_ms=120_000)
        return runtime

    def started(self, *, mark_price: bool = False, **kwargs: object) -> ProductRuntime:
        """启动真实 runtime（等 market state 变为 tradeable 后返回）。"""
        runtime = self.runtime(**kwargs)                                   # type: ignore[arg-type]
        if mark_price:
            self.inject_mark_price(runtime)                                # 启动前注入 Risk 事实
        runtime.start()
        self.started_runtimes.append(runtime)
        provider = runtime._feed_provider                                       # noqa: SLF001

        def tradeable() -> bool:
            quality = getattr(getattr(provider.last_state, "quality", None), "tradeable", False)
            return bool(quality)

        self.assertTrue(wait_for(tradeable, timeout=30.0), "market state never became tradeable")
        return runtime

    @staticmethod
    def inject_mark_price(runtime: ProductRuntime, price: float = MARK_PRICE) -> None:
        """经既有 Owner 契约注入 mark price（不使用 last trade；ACCOUNTING 拥有该事实）。"""
        runtime._execution.accounting.update_mark_price(                   # noqa: SLF001
            "BTCUSDT", price, timestamp=int(runtime.profile.clock()))

    # ------------------------------------------------------------------ PAPER 闭环

    def test_paper_natural_decision_reaches_execution_through_the_risk_gate(self) -> None:
        runtime = self.started(mark_price=True)
        loop = runtime._decision_loop                                           # noqa: SLF001

        self.assertTrue(wait_for(lambda: loop.status.submits >= 2, timeout=30.0),
                        f"no natural submit: {loop.status.__dict__}")
        snapshot = runtime.service.snapshot()

        # 决策链真实发生（不是手工 smoke order，也不是伪装订单）
        self.assertTrue(wait_for(lambda: loop.latest_decision is not None, timeout=30.0))
        decision = loop.latest_decision
        self.assertEqual(decision.mode.value, "both")               # 双边报价姿态
        self.assertIn(decision.bid.action, (QuoteAction.PLACE, QuoteAction.KEEP, QuoteAction.REPLACE))
        self.assertGreaterEqual(loop.status.decisions, 1)
        self.assertEqual(loop.status.risk_rejects, 0)
        self.assertIsNotNone(loop.latest_prediction)

        # 订单真实进入 execution（唯一本地适配器）+ RiskGate 逐单 allow
        orders = tuple(snapshot.execution.active_orders)
        self.assertGreaterEqual(len(orders), 1)
        self.assertTrue(all(order.status in ("OPEN", "PARTIALLY_FILLED") for order in orders))
        # P0001.17：事件级模拟成交可能已经把其中一侧打掉 ⇒ 用 Risk allow 证据证明两单都过了 RiskGate
        submitted_ids = [str(stage.identity.value) for stage in snapshot.evidence.trace
                         if stage.stage == "order"]
        self.assertGreaterEqual(len(submitted_ids), 2)
        risk_stages = [stage for stage in snapshot.evidence.trace if stage.stage == "risk"]
        self.assertEqual([str(stage.outcome) for stage in risk_stages], ["allow", "allow"])
        order_stages = [stage for stage in snapshot.evidence.trace if stage.stage == "order"]
        self.assertTrue(order_stages)
        self.assertTrue(all(stage.outcome == "OPEN" for stage in order_stages))

    def test_paper_records_readiness_but_does_not_use_it_as_a_submit_gate(self) -> None:
        runtime = self.started(mark_price=True)
        loop = runtime._decision_loop                                           # noqa: SLF001
        self.assertTrue(wait_for(lambda: loop.status.submits >= 1, timeout=30.0))

        fact = loop.readiness_result
        snapshot = runtime.service.snapshot()

        self.assertEqual(fact.status, "unavailable")
        self.assertIn(PAPER_READINESS_REASON, fact.reasons)
        self.assertEqual(loop.status.readiness_blocks, 0)
        self.assertEqual(str(snapshot.readiness.status.value), "unavailable")
        self.assertEqual(snapshot.readiness.reasons, (PAPER_READINESS_REASON,))
        self.assertFalse(snapshot.readiness.applicable.value)        # 明确标记"不适用"
        self.assertTrue(snapshot.execution.active_orders)            # 未被 readiness 阻断（方案 A）

        stage = next(stage for stage in snapshot.evidence.trace if stage.stage == "readiness")
        self.assertEqual(str(stage.outcome), "not_applicable")       # 不是 "blocked"
        owners = [getattr(b.owner, "value", b.owner) for b in snapshot.blockers]
        self.assertNotIn("READINESS", owners)                        # 不制造假 readiness blocker

    def test_without_mark_price_the_chain_fails_closed_and_reports_why(self) -> None:
        runtime = self.started()
        loop = runtime._decision_loop                                           # noqa: SLF001

        self.assertTrue(wait_for(lambda: loop.status.decisions >= 1, timeout=30.0))
        time.sleep(1.0)
        snapshot = runtime.service.snapshot()
        decision = loop.latest_decision

        self.assertEqual(loop.status.submits, 0)                     # 无 mark price ⇒ 无预算 ⇒ 不下单
        self.assertEqual(snapshot.execution.active_orders, ())
        self.assertEqual(decision.mode.value, "none")
        self.assertEqual(decision.bid.action, QuoteAction.NONE)
        self.assertEqual(str(decision.bid.trigger.value), "risk_budget_unknown")
        maker = next(stage for stage in snapshot.evidence.trace if stage.stage == "maker_decision")
        self.assertEqual(str(maker.outcome), "none")
        self.assertEqual(str(maker.reason_code.value), "RISK_BUDGET_UNKNOWN")
        self.assertEqual(snapshot.strategy.blocked_by.value, "RISK_BUDGET_UNKNOWN")
        # 真实原因必须在 catalog 里可解释（UI 不显示"没有原因"）
        self.assertIsNotNone(reason_catalog.lookup("RISK_BUDGET_UNKNOWN"))

    def test_unavailable_prediction_provider_is_reported_honestly(self) -> None:
        runtime = self.started(mark_price=True, provider=False)
        loop = runtime._decision_loop                                           # noqa: SLF001

        self.assertTrue(wait_for(lambda: loop.status.decisions >= 1, timeout=30.0))
        snapshot = runtime.service.snapshot()

        self.assertEqual(loop.status.predictions_submitted, 0)
        self.assertIsNone(loop.latest_prediction)
        self.assertEqual(runtime._prediction_runtime, None)                     # noqa: SLF001
        stages = [stage for stage in snapshot.evidence.trace if stage.stage == "prediction"]
        self.assertEqual([str(stage.outcome) for stage in stages], ["absent"])
        self.assertEqual(snapshot.execution.active_orders, ())                  # 不伪造报价

    def test_missing_maker_config_produces_no_decision_instead_of_a_fake_one(self) -> None:
        runtime = self.started(mark_price=True, maker=False)
        loop = runtime._decision_loop                                           # noqa: SLF001

        self.assertTrue(wait_for(lambda: loop.status.ticks >= 5, timeout=30.0))
        time.sleep(1.0)
        snapshot = runtime.service.snapshot()

        self.assertIsNone(runtime._maker_policy)                                # noqa: SLF001
        self.assertIsNone(loop.latest_decision)
        self.assertEqual(loop.status.decisions, 0)
        self.assertEqual(snapshot.execution.active_orders, ())
        stages = [stage for stage in snapshot.evidence.trace if stage.stage == "maker_decision"]
        self.assertEqual([str(stage.outcome) for stage in stages], ["absent"])
        self.assertIsNone(snapshot.strategy.mode.value)                          # 如实 UNKNOWN

    # ------------------------------------------------------------------ 装配 / 边界

    def test_product_providers_expose_real_runtime_facts(self) -> None:
        runtime = self.started(mark_price=True)
        loop = runtime._decision_loop                                           # noqa: SLF001
        self.assertTrue(wait_for(lambda: loop.status.decisions >= 1, timeout=30.0))

        snapshot = runtime.service.snapshot()

        self.assertTrue(snapshot.prediction.request_id.known)
        self.assertEqual(snapshot.prediction.provider.value, "fake-jev")
        self.assertTrue(snapshot.risk.max_position_notional.known)              # 不再 lambda: None
        self.assertEqual(snapshot.risk.max_position_notional.value, 100.0)
        self.assertTrue(snapshot.strategy.mode.known)
        self.assertTrue(snapshot.readiness.status.known)
        self.assertTrue(snapshot.prediction.freshest.known)

    def test_exactly_one_paper_broker_owns_execution(self) -> None:
        runtime = self.started()

        provider = runtime._feed_provider                                       # noqa: SLF001
        self.assertFalse(hasattr(provider, "paper_broker"))                     # feed 不拥有 broker
        self.assertFalse(hasattr(provider, "paper_manager"))
        # P0001.15 §9：engine 依赖统一 connector seam，唯一 PaperBroker 由 connector 持有
        adapter = runtime._execution.manager.adapter                          # noqa: SLF001
        self.assertIsInstance(adapter, PaperExecutionConnector)
        # P0001.17：本地适配器 = SimulatedVenue（事件级模拟成交）或 PaperBroker（手工 fill），且只有一个
        self.assertIsInstance(adapter.broker, (PaperBroker, SimulatedVenue))

        sources = {path.name: path.read_text(encoding="utf-8")
                   for path in (Path(__file__).resolve().parents[2] / "runtime").glob("*.py")}
        construction = sorted(name for name, text in sources.items()
                              if "PaperBroker(" in text or "SimulatedVenue(" in text)
        self.assertEqual(construction, ["assembly.py"])                         # 唯一构造点

    def test_decision_loop_has_no_venue_connector_coupling(self) -> None:
        text = (Path(__file__).resolve().parents[2] / "runtime" / "decision_loop.py").read_text()

        for forbidden in ("connectors", "binance", "PaperBroker", "ccxt", "requests"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_replay_is_observe_only(self) -> None:
        runtime = self.started(mode=RuntimeMode.REPLAY, mark_price=True)
        loop = runtime._decision_loop                                           # noqa: SLF001

        self.assertTrue(wait_for(lambda: loop.status.decisions >= 1, timeout=30.0))
        self.assertFalse(loop.config.write_enabled)                             # 只观察，不写入
        self.assertEqual(loop.status.submits, 0)
        self.assertEqual(runtime.service.snapshot().execution.active_orders, ())

    def test_stop_is_graceful_and_leaves_no_thread_behind(self) -> None:
        runtime = self.started()
        loop = runtime._decision_loop                                           # noqa: SLF001
        self.assertTrue(loop.running)

        runtime.stop()
        runtime.stop()                                                          # 幂等

        self.assertFalse(loop.running)
        self.assertEqual([t for t in threading.enumerate() if "probex-decision" in t.name], [])


if __name__ == "__main__":
    unittest.main()
