"""P0001.15 验收（A/B/D/E + SC-24 … SC-28）：instrument identity、MARK reference price、correlation、connector health。

真实装配（`ProductRuntime`），不使用手工构造的 service：

- **SC-24**：含 `MARK_PRICE` 事件的 fixture → `ReferencePriceProvider` → Risk → PAPER execution 工程闭环；
- **SC-25**：不含 `MARK_PRICE` → `ReferencePrice UNKNOWN` → fail-closed；
- **SC-26**：`decision_id` 是 canonical order correlation metadata（不是 runtime side-map）；
- **SC-27**：`Order → Decision` 与 `Decision → Order` 双向可查（快照 + API + tracker）；
- **SC-28**：market / private connector health 独立（可出现不同状态，不合并）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from domain.instruments import AssetClass, PriceType, ProductType
from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile
from strategy.maker.types import QuoteAction
from venue.health import ConnectionState

MARK_PRICE = 60_000.0

#: 测试用 MakerPolicy 参数（测试值；生产值必须由 operator/profile 显式提供）
MAKER_VALUES: dict[str, float] = {
    "strategy.maker.tick_size": 0.1, "strategy.maker.quantity_step": 0.0001,
    "strategy.maker.min_quote_size": 0.0001, "strategy.maker.base_size": 0.001,
    "strategy.maker.max_back_ticks": 5, "strategy.maker.minimum_edge_bps": 0.0,
    "strategy.maker.prediction_horizon_ms": 15_000, "strategy.maker.adverse_selection_retreat": 0.6,
    "strategy.maker.adverse_selection_block": 0.99, "strategy.maker.imbalance_retreat": 0.5,
    "strategy.maker.microprice_skew_retreat_bps": 5.0, "strategy.maker.predicted_move_retreat": 0.5,
    "strategy.maker.target_position": 0.0, "strategy.maker.inventory_scale": 0.01,
    "strategy.maker.inventory_size_strength": 0.5, "strategy.maker.inventory_retreat_ticks_max": 3,
    "strategy.maker.size_factor_min": 0.25, "strategy.maker.size_factor_max": 1.0,
    "strategy.maker.confidence_ref_low": 0.2, "strategy.maker.confidence_ref_high": 0.8,
    "strategy.maker.confidence_factor_min": 0.25, "strategy.maker.risk_factor_min": 0.25,
    "strategy.maker.price_move_ticks_replace": 1, "strategy.maker.max_quote_age_ms": 30_000,
    "strategy.maker.size_drift_tolerance": 0.1,
}
RISK_VALUES: dict[str, float] = {"risk.max_position_notional": 100.0, "risk.max_position_qty": 0.01}


def wait_for(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class VenueIntegrationE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probex-p0001.15-"))
        self.started: list[ProductRuntime] = []

    def tearDown(self) -> None:
        for runtime in self.started:
            if runtime._session.record.status.value == "RUNNING":       # noqa: SLF001
                runtime.stop()
        self.started.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers

    def runtime(self, *, mode: RuntimeMode = RuntimeMode.PAPER, mark_price: float | None = MARK_PRICE,
                provider: bool = True, hours: int = 1) -> ProductRuntime:
        from tests.fakes import FakeProvider
        from tests.ui.market_fixture import write_market_store

        store = self.tmp / f"events-{mode.value}-{'mark' if mark_price else 'nomark'}.jsonl"
        write_market_store(store, hours=hours, mark_price=mark_price)
        values: dict[str, object] = json.loads(
            (Path(__file__).resolve().parents[2] / "profiles" / "trial-local.json").read_text())
        values.update(MAKER_VALUES)
        values.update(RISK_VALUES)
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

    def started_runtime(self, **kwargs: object) -> ProductRuntime:
        runtime = self.runtime(**kwargs)                                   # type: ignore[arg-type]
        runtime.start()
        self.started.append(runtime)
        provider = runtime._feed_provider                                        # noqa: SLF001

        def tradeable() -> bool:
            quality = getattr(getattr(provider.last_state, "quality", None), "tradeable", False)
            return bool(quality)

        self.assertTrue(wait_for(tradeable, timeout=30.0), "market state never became tradeable")
        return runtime

    # ------------------------------------------------------------------ A: instrument identity

    def test_a_instrument_identity_is_explicit_and_shared_by_api_and_snapshot(self) -> None:
        runtime = self.started_runtime()
        snapshot = runtime.service.snapshot()

        self.assertTrue(snapshot.instrument.instrument_id.known)
        self.assertEqual(snapshot.instrument.instrument_id.value, "paper:BTCUSDT")
        self.assertEqual(snapshot.instrument.asset_class.value, AssetClass.CRYPTO.value)
        self.assertEqual(snapshot.instrument.product_type.value, ProductType.PERPETUAL.value)
        self.assertEqual(snapshot.instrument.base_asset.value, "BTC")
        self.assertEqual(snapshot.instrument.quote_asset.value, "USDT")
        self.assertEqual(snapshot.instrument.settlement_asset.value, "USDT")
        self.assertTrue(snapshot.instrument.production_ready.value)
        capabilities = snapshot.instrument.capabilities.value
        self.assertTrue(capabilities["supports_short"] and capabilities["supports_reduce_only"])
        self.assertTrue(capabilities["has_funding"] and not capabilities["has_expiry"])
        self.assertEqual(snapshot.instrument.reference_price_policy.value["risk_price_types"], ["MARK"])

        # venue identity 与 instrument 分离
        self.assertEqual(snapshot.venue.venue_id.value, "paper")
        self.assertEqual(snapshot.venue.venue_type.value, "paper")
        self.assertEqual(snapshot.venue.environment.value, "PAPER")

        # 同一事实在 API 上可见
        server, base = self._serve(runtime)
        try:
            status, payload = self._get(base, "/api/v1/instrument")
            self.assertEqual(status, 200)
            self.assertEqual(payload["instrument"]["instrument_id"]["value"], "paper:BTCUSDT")
            self.assertEqual(payload["instrument"]["asset_class"]["value"], "CRYPTO")
            self.assertEqual(payload["instrument"]["product_type"]["value"], "PERPETUAL")
            self.assertEqual(payload["venue"]["venue_id"]["value"], "paper")
        finally:
            self._close(server, runtime)

    # ------------------------------------------------------------------ B / SC-24: MARK closed loop

    def test_sc24_mark_fixture_drives_reference_price_risk_and_paper_execution(self) -> None:
        runtime = self.started_runtime(mark_price=MARK_PRICE)
        loop = runtime._decision_loop                                           # noqa: SLF001
        self.assertTrue(wait_for(lambda: loop.status.submits >= 2, timeout=30.0),
                        f"no natural submit: {loop.status.__dict__}")
        snapshot = runtime.service.snapshot()

        # 1) 正式 MARK 事件 → reference price（known + source + as_of）
        reference = snapshot.reference_price
        self.assertTrue(reference.known.value, reference.reason.value)
        self.assertEqual(reference.price_type.value, PriceType.MARK.value)
        self.assertEqual(reference.price.value, MARK_PRICE)
        self.assertIn("mark_price", str(reference.source.value))
        self.assertTrue(reference.as_of.known)
        self.assertIsNotNone(reference.freshness_ms.value)

        # 2) reference price → Risk（accounting 收的是正式 MARK，不是 last trade）
        accounting = runtime._execution.accounting                               # noqa: SLF001
        self.assertEqual(accounting.mark_price("BTCUSDT"), MARK_PRICE)

        # 3) Risk 允许 → PAPER execution connector → 唯一 PaperBroker
        self.assertEqual(loop.status.risk_rejects, 0)
        orders = snapshot.execution.active_orders
        self.assertTrue(orders)
        self.assertTrue(all(order.status == "OPEN" for order in orders))
        risk_stages = [stage for stage in snapshot.evidence.trace if stage.stage == "risk"]
        self.assertTrue(risk_stages)
        self.assertEqual([str(stage.outcome) for stage in risk_stages], ["allow"] * len(risk_stages))

    def test_sc25_missing_mark_keeps_reference_price_unknown_and_fails_closed(self) -> None:
        runtime = self.started_runtime(mark_price=None)
        loop = runtime._decision_loop                                           # noqa: SLF001
        self.assertTrue(wait_for(lambda: loop.status.decisions >= 1, timeout=30.0))
        time.sleep(0.5)
        snapshot = runtime.service.snapshot()

        reference = snapshot.reference_price
        self.assertFalse(reference.known.value)                                # UNKNOWN 不是 0
        self.assertIsNone(reference.price.value)
        self.assertIsNone(reference.as_of.value)
        self.assertEqual(reference.reason.value, "REFERENCE_PRICE_MARK_UNAVAILABLE")
        # fail-closed：没有 mark ⇒ 没有预算 ⇒ 不下单（且不把 last trade 当 mark）
        self.assertEqual(loop.status.submits, 0)
        self.assertEqual(snapshot.execution.active_orders, ())
        self.assertEqual(loop.latest_decision.mode.value, "none")
        self.assertEqual(loop.latest_decision.bid.action, QuoteAction.NONE)
        self.assertEqual(runtime._execution.accounting.mark_price("BTCUSDT"), None)   # noqa: SLF001

    # ------------------------------------------------------------------ E / SC-26 / SC-27: correlation

    def test_sc26_sc27_decision_and_order_are_linked_through_canonical_correlation(self) -> None:
        runtime = self.started_runtime(mark_price=MARK_PRICE)
        loop = runtime._decision_loop                                           # noqa: SLF001
        self.assertTrue(wait_for(lambda: loop.status.submits >= 2, timeout=30.0))
        snapshot = runtime.service.snapshot()
        order = snapshot.execution.active_orders[0]

        # Order → Decision（来自订单自身的 correlation）
        self.assertTrue(order.decision_id.known)
        decision_id = order.decision_id.value
        self.assertEqual(order.instrument_id.value, "paper:BTCUSDT")
        self.assertEqual(order.venue_id.value, "paper")
        self.assertTrue(order.prediction_id.known)

        # 订单记录里确实存着 correlation（不是 runtime / product 侧映射）
        correlation = runtime._tracker_owner.order(order.client_order_id).correlation    # noqa: SLF001
        self.assertIsNotNone(correlation)
        self.assertEqual(correlation.decision_id, decision_id)
        self.assertEqual(correlation.instrument_id, "paper:BTCUSDT")
        self.assertEqual(correlation.venue_id, "paper")
        # market_state_hash 是决策所做状态的内容寻址指纹（与 loop 记录的 state hash 一致）
        self.assertTrue(str(correlation.market_state_hash).startswith("sha256:"))
        self.assertEqual(correlation.market_state_hash, loop.status.last_state_hash)

        # Decision → Order(s)（tracker 的 canonical correlation 反查；同一 decision 的订单都返回）
        linked = runtime._execution.orders_for_decision(decision_id)            # noqa: SLF001
        self.assertTrue(linked)
        self.assertTrue(all(o.correlation.decision_id == decision_id for o in linked))
        self.assertTrue(all(o.correlation.instrument_id == "paper:BTCUSDT" for o in linked))

        # Product API / UI 也可双向查询（SC-27）
        server, base = self._serve(runtime)
        try:
            status, payload = self._get(base, f"/api/v1/decisions/orders?decision_id={decision_id}")
            self.assertEqual(status, 200)
            self.assertEqual(payload["decision_id"], decision_id)
            self.assertGreaterEqual(payload["count"], 1)
            self.assertEqual(payload["orders"][0]["decision_id"]["value"], decision_id)
            status, orders_payload = self._get(base, "/api/v1/orders")
            self.assertEqual(status, 200)
            self.assertIn("active_orders", orders_payload["execution"])
            # SC-27：`Decision → Order(s)` 与订单是否仍 active 无关（用 canonical correlation 反查）
            matched = next(item for item in payload["orders"]
                           if item["client_order_id"] == order.client_order_id)
            self.assertEqual(matched["decision_id"]["value"], decision_id)
            self.assertEqual(matched["instrument_id"]["value"], "paper:BTCUSDT")
            self.assertEqual(matched["venue_id"]["value"], "paper")
        finally:
            self._close(server, runtime)

    # ------------------------------------------------------------------ A / G: no side maps, no venue coupling

    def test_sc26_runtime_holds_no_decision_to_order_mapping(self) -> None:
        runtime = self.started_runtime(mark_price=MARK_PRICE)
        loop = runtime._decision_loop                                           # noqa: SLF001
        self.assertTrue(wait_for(lambda: loop.status.submits >= 2, timeout=30.0))

        # 决策 loop 不持有任何 correlation / client_order_id 映射（事实只在订单记录里）
        for name in ("_decision_orders", "_decision_index", "_order_ids_by_decision", "_correlations"):
            self.assertFalse(hasattr(loop, name), name)
        self.assertFalse(hasattr(loop.status, "decision_orders"))
        # 产品层同样没有 side mapping
        self.assertFalse(hasattr(runtime.service, "_decision_index"))

    # ------------------------------------------------------------------ C / SC-28: split health

    def test_sc28_market_and_private_connector_health_are_independent(self) -> None:
        runtime = self.started_runtime()
        snapshot = runtime.service.snapshot()

        market = snapshot.market_connector_health
        private = snapshot.private_connector_health
        self.assertEqual(market.kind, "market")
        self.assertEqual(private.kind, "private")
        self.assertEqual(market.connector_id.value, "paper:market")
        self.assertEqual(private.connector_id.value, "paper:execution")
        # 两个 connector 各自暴露状态；不会出现单一 `connected` 字段
        self.assertTrue(market.connection_state.known)
        self.assertTrue(private.connection_state.known)
        self.assertNotIn("connected", private.extras.value or {})
        self.assertNotIn("connected", market.extras.value or {})

        # 两个 connector 各自独立：状态各自来自自己的生命周期与观测事实
        self.assertIs(market.connection_state.value, ConnectionState.CONNECTED.value)
        self.assertIs(private.connection_state.value, ConnectionState.CONNECTED.value)
        self.assertIsNone(private.last_event_ms.value)      # 未观测 ⇒ UNKNOWN，不推断"无成交"
        self.assertFalse(private.observed.value)
        self.assertTrue(market.observed.value)

    def test_sc28_connector_health_can_diverge(self) -> None:
        runtime = self.started_runtime()
        connector = runtime._execution_connector                                # noqa: SLF001
        connector.disconnect()                                                  # 只有 private 断开
        snapshot = runtime.service.snapshot()

        self.assertEqual(snapshot.private_connector_health.connection_state.value,
                         ConnectionState.DISCONNECTED.value)
        self.assertEqual(snapshot.market_connector_health.connection_state.value,
                         ConnectionState.CONNECTED.value)
        self.assertFalse(hasattr(snapshot, "connected"))

    # ------------------------------------------------------------------ B: single broker

    def test_b_paper_execution_goes_through_the_unified_connector_with_one_broker(self) -> None:
        from connectors.paper import PaperExecutionConnector
        from execution.adapters.paper import PaperBroker
        from execution.simulation.venue import SimulatedVenue

        runtime = self.started_runtime(mark_price=MARK_PRICE)
        adapter = runtime._execution.manager.adapter                            # noqa: SLF001
        self.assertIsInstance(adapter, PaperExecutionConnector)
        # P0001.17：本地适配器 = SimulatedVenue（事件级模拟成交）或 PaperBroker（手工 fill），且只有一个
        self.assertIsInstance(adapter.broker, (PaperBroker, SimulatedVenue))
        self.assertEqual(adapter.venue_identity.venue_id, runtime._venue_identity.venue_id)  # noqa: SLF001
        self.assertFalse(hasattr(runtime._feed_provider, "paper_broker"))       # noqa: SLF001

    # ------------------------------------------------------------------ D: reference price contract

    def test_d_reference_price_never_substitutes_last_trade(self) -> None:
        """`LAST` 结构性不可用于风险路径（策略/政策层保证，不靠运行时兜底）。"""
        runtime = self.started_runtime(mark_price=None)
        registry = runtime._resolve_instrument_registry()                       # noqa: SLF001
        policy = registry.current.reference_price_policy

        self.assertEqual(policy.risk_price_types, (PriceType.MARK,))
        self.assertIn(PriceType.LAST, policy.observable_price_types)            # 可表达
        self.assertFalse(policy.accepts_for_risk(PriceType.LAST))               # 不可用于风险
        self.assertFalse(policy.allows_risk_substitution)

    # ------------------------------------------------------------------ 内部

    def _serve(self, runtime: ProductRuntime):
        import threading

        server = runtime.create_server()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def _close(self, server, runtime: ProductRuntime) -> None:
        server.shutdown()
        server.server_close()

    def _get(self, base: str, path: str) -> tuple[int, dict]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f"{base}{path}")
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
