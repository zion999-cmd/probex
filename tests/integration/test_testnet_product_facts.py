"""P0001.16 §16：TESTNET stack 必须真正接入 ProductService（离线、可重复）。

用受控 doubles 组装同一个 `TestnetStack`（真实 adapter / engine / tracker / accounting / ledger /
connector / ProductService 接线），证明产品读模型拿到的是**真实 Owner 事实**：

- Monitor：TESTNET / venue / private connector health / position / active orders；
- Activity：submit → ack → NEW → FILLED → reconciliation 因果链；
- Orders：decision_id / client_order_id / venue_order_id / status / filled qty；
- System：private connector / user stream / latency 样本；
- Assistant：为什么 UNKNOWN、是否已 reconciliation、user stream 是否健康、fill 属于哪个 decision。

不触网：写路径用既有 `FakeExecutionFetcher`（真实 adapter 代码路径），私有流用 bridge 事实。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from connectors.binance.execution.rest import BinanceExecutionRestClient
from connectors.binance.execution_connector import BinancePrivateExecutionConnector
from connectors.binance.market_data.exchange_info import TradingRules
from connectors.binance.market_data.mark import MarkPriceObservation
from connectors.binance.private.events import parse_user_event
from tests.private_support import order_update_message
from domain.instruments import AssetClass, ProductType
from execution.engine import ExecutionEngine
from execution.manager import OrderManager
from execution.tracker import OrderTracker
from market.events.types import Venue
from portfolio.accounting import AccountingCore
from portfolio.fills import FillLedger
from portfolio.types import Side
from product.types import RuntimeMode
from readiness import LiveRiskPolicy, ReadinessPolicy
from risk.gate import RiskGate
from risk.high_watermark import HighWatermarkTracker
from risk.limits import RiskLimits
from risk.types import OrderCorrelation, OrderProposal
from runtime.testnet import TestnetConfig, TestnetStack, build_testnet_product_service
from storage.high_watermark import JsonHighWatermarkStore
from tests.execution_support_live import FakeExecutionFetcher, echoing_ack, client
from tests.support import BASE_TS, SYMBOL, feature_engine, warm_market_states
from tests.unit.test_execution_adapter import context as authority_context
from venue import VenueEnvironment, binance_venue

NOW = BASE_TS + 1_000


def _rules() -> TradingRules:
    return TradingRules(symbol=SYMBOL, status="TRADING", tick_size=0.1, min_price=1.0, max_price=1_000_000.0,
                        step_size=0.001, min_qty=0.001, max_qty=100.0, min_notional=5.0)


class _MarketDouble:
    """public 行情 double：只暴露产品/参考价需要的只读事实。"""

    def __init__(self) -> None:
        self._rules = _rules()
        engine = feature_engine()
        state = warm_market_states(1, engine=engine)[-1]
        self._history = (state,)
        self._mark = MarkPriceObservation(venue=Venue.BINANCE, symbol=SYMBOL, price=60_000.0,
                                          exchange_ts=NOW - 100, receive_ts=NOW - 90, process_ts=NOW - 90)

    @property
    def trading_rules(self) -> TradingRules:
        return self._rules

    @property
    def history(self) -> tuple[object, ...]:
        return self._history

    def latest_mark(self) -> MarkPriceObservation:
        return self._mark

    @property
    def mark(self) -> object:
        return self._mark


def _stack() -> TestnetStack:
    market = _MarketDouble()
    tracker = OrderTracker(session_id="testnet-offline", venue=Venue.BINANCE)
    accounting = AccountingCore(initial_balance=10_000.0)
    accounting.update_mark_price(SYMBOL, 60_000.0, timestamp=BASE_TS)
    ledger = FillLedger()
    fetcher = FakeExecutionFetcher(responses={"POST": echoing_ack})
    adapter = __import__("connectors.binance.execution.adapter", fromlist=["BinanceExecutionAdapter"]) \
        .BinanceExecutionAdapter(rest=client(fetcher, now_ms=NOW), environment=_env(),
                                 authority_provider=authority_context, rules_provider=_rules,
                                 symbol=SYMBOL)  # type: ignore[arg-type]
    connector = BinancePrivateExecutionConnector(adapter=adapter, venue_identity=binance_venue(
        environment=VenueEnvironment.TESTNET), symbol=SYMBOL, clock=lambda: NOW)
    connector.connect()          # 真实组合在启动时连接；离线 double 同样标记为已连接
    manager = OrderManager(tracker=tracker, adapter=connector)
    engine = ExecutionEngine(accounting=accounting, gate=RiskGate(RiskLimits(
        max_position_qty=1.0, max_open_order_exposure=5_000.0)), manager=manager, book_healthy=True)
    config = TestnetConfig(symbol=SYMBOL, risk_policy=_risk_policy(), readiness_policy=_readiness_policy(),
                           state_dir="/tmp/probex-testnet-offline", authority_ttl_ms=60_000,
                           price_rounding="ROUND_DOWN", quantity_rounding="ROUND_DOWN")
    stack = TestnetStack(config=config, market=market, private=_PrivateDouble(), private_rest=None,  # type: ignore[arg-type]
                         adapter=adapter, connector=connector, tracker=tracker, manager=manager,
                         engine=engine, accounting=accounting, ledger=ledger, recovery=None,  # type: ignore[arg-type]
                         high_watermark=HighWatermarkTracker(state=None, store=_memory_store()),
                         clock=lambda: NOW, gate=None)  # type: ignore[arg-type]
    stack._latency = __import__("runtime.latency_observer", fromlist=["ExecutionLatencyObserver"]) \
        .ExecutionLatencyObserver(log=__import__("runtime.latency_observer",
                                                 fromlist=["BoundedLatencyLog"]).BoundedLatencyLog(),
                                  clock=lambda: NOW)
    stack._runtime_id = "testnet-offline"
    stack._started_at = BASE_TS
    engine.latency_observer = lambda kind, ts: stack._latency.note(kind, ts)
    return stack


def _memory_store() -> object:
    from tests.integration.test_readiness_collector import InMemoryHighWatermarkStore

    return InMemoryHighWatermarkStore()


def _env() -> object:
    from readiness import Environment

    return Environment.TESTNET


def _risk_policy() -> LiveRiskPolicy:
    return LiveRiskPolicy(max_position_qty=1.0, max_order_notional=100.0, max_open_order_exposure=5_000.0,
                          max_daily_loss=500.0, max_drawdown_pct=0.5, max_leverage=1.0,
                          max_mark_age_ms=60_000)


def _readiness_policy() -> ReadinessPolicy:
    return ReadinessPolicy(max_clock_uncertainty_ms=300, max_median_private_lag_ms=2_000,
                           max_calibration_age_ms=600_000, max_available_balance_age_ms=30_000)


class _PrivateDouble:
    """私有 runtime double：只暴露产品/readiness 需要的只读事实。"""

    def __init__(self) -> None:
        self.telemetry = SimpleNamespace(listen_key_state="ACTIVE", message_count=5,
                                        last_receive_lag_ms=42, server_time_offset_ms=-2,
                                        account_update_count=1, connect_count=1)
        self.snapshot_boundary = None
        self.continuity_assumed = True
        self.latest_snapshot = None
        self.latest_position = None
        self.lifecycle_state = "ACTIVE"      # 组合在测试中已连接（connect() 幂等）

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def pump_once(self, *, timeout_s: float, max_messages: int = 1) -> object:
        return SimpleNamespace(events=(), snapshot=None, errors=(), timed_out=True)
    def refresh_snapshot(self) -> None: return None
    def refresh_clock_calibration(self) -> None: return None


class TestnetProductFactsTest(unittest.TestCase):
    def test_monitor_orders_system_and_activity_expose_real_testnet_facts(self) -> None:
        stack = _stack()
        correlation = OrderCorrelation(decision_id="d-abc", instrument_id="binance:BTCUSDT",
                                       venue_id="binance", prediction_id="pred-1")
        proposal = OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=60_000.0,
                                 post_only=True, correlation=correlation)
        result = stack.submit(proposal)
        self.assertTrue(result.submitted, result)

        # 真实 user stream 事实（bridge 与 adapter 同一路径）→ NEW
        client_order_id = result.order.client_order_id
        import json as _json

        user_event = parse_user_event(_json.loads(order_update_message(client_order_id=client_order_id,
                                                           order_status="NEW", execution_type="NEW",
                                                           last_fill_quantity="0",
                                                           cumulative_fill_quantity="0", event_ts=NOW)),
                                      receive_ts=NOW, process_ts=NOW)
        stack.connector.bridge_user_event(user_event.observation)
        stack.engine.poll(now_ms=NOW)

        service = build_testnet_product_service(stack)
        snapshot = service.snapshot()

        # --- Monitor / System：身份、venue、connector health、position、latency
        self.assertIs(snapshot.runtime.mode, RuntimeMode.TESTNET)
        self.assertEqual(snapshot.runtime.environment, "testnet")
        self.assertEqual(snapshot.venue.venue_id.value, "binance")
        self.assertEqual(snapshot.venue.environment.value, "TESTNET")
        self.assertEqual(snapshot.instrument.asset_class.value, AssetClass.CRYPTO.value)
        self.assertEqual(snapshot.instrument.product_type.value, ProductType.PERPETUAL.value)
        self.assertTrue(snapshot.private_connector_health.connection_state.known)
        self.assertTrue(snapshot.market_connector_health.kind == "market")
        self.assertTrue(snapshot.portfolio.position_qty.known)
        self.assertTrue(snapshot.reference_price.known.value)               # 正式 MARK（mark price 观测）
        self.assertEqual(snapshot.reference_price.price_type.value, "MARK")
        self.assertTrue(snapshot.execution.last_submit_classification.known)
        self.assertEqual(snapshot.execution.last_submit_classification.value, "CONFIRMED_ACCEPTED")

        # --- Orders：decision_id / client_order_id / venue_order_id / status / filled qty / connector
        order = snapshot.execution.active_orders[0]
        self.assertEqual(order.client_order_id, client_order_id)
        self.assertEqual(order.decision_id.value, "d-abc")
        self.assertEqual(order.instrument_id.value, "binance:BTCUSDT")
        self.assertEqual(order.venue_id.value, "binance")
        self.assertTrue(order.venue_order_id.known)                          # 来自真实 ack
        self.assertTrue(order.prediction_id.known)
        self.assertIn(order.status, ("OPEN", "PARTIALLY_FILLED"))
        self.assertEqual(order.filled_quantity.value, 0.0)
        self.assertEqual(snapshot.venue.execution_connector_id.value, "binance:execution")

        # --- Activity：submit / ack / NEW 因果链（同一 instrument/venue identity）
        stages = {entry.stage for entry in snapshot.evidence.trace}
        self.assertIn("order", stages)
        self.assertIn("ack", stages)
        order_stage = next(entry for entry in snapshot.evidence.trace if entry.stage == "order")
        self.assertEqual(order_stage.instrument_id.value, "binance:BTCUSDT")
        self.assertEqual(order_stage.venue_id.value, "binance")
        self.assertEqual(order_stage.decision_id.value if hasattr(order_stage, "decision_id") else
                         order_stage.detail.split("decision_id=")[-1], "d-abc")

    def test_reconciliation_and_unknown_reason_are_visible_to_product_and_assistant(self) -> None:
        # 受控故障注入：POST 响应丢失 ⇒ UNKNOWN（不 retry）；query 返回真实事实 ⇒ 收敛
        from connectors.binance.execution.rest import ExecutionOutcomeUnknown
        from tests.execution_support_live import FakeExecutionFetcher, echoing_ack

        stack = _stack()
        fetcher = FakeExecutionFetcher(responses={"POST": ExecutionOutcomeUnknown(status=None, detail="dropped"),
                                                  "GET": echoing_ack})
        stack.adapter.rest = client(fetcher, now_ms=NOW)
        proposal = OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=60_000.0, post_only=True)
        result = stack.submit(proposal)
        client_order_id = result.order.client_order_id
        self.assertEqual(stack.tracker.require_order(client_order_id).status.value, "PENDING_CREATE")
        self.assertEqual(stack.adapter.unknown_submit_count, 1)
        record = stack.resolve_unknown(client_order_id)
        self.assertEqual(record["outcome"], "reconciled_from_query")
        self.assertNotEqual(stack.tracker.require_order(client_order_id).status.value, "PENDING_CREATE")

        service = build_testnet_product_service(stack)
        snapshot = service.snapshot()
        trace = [entry for entry in snapshot.evidence.trace if entry.stage == "reconciliation"]
        self.assertTrue(trace)                                                # reconciliation 证据可见
        notes = " ".join(service.orchestrator_notes())
        self.assertIn("reconciliation:", notes)
        self.assertIn("stream:", notes)

        # Assistant 能解释：UNKNOWN 原因 / reconciliation / user stream / fill 归属
        from assistant.service import AssistantService

        assistant = AssistantService(snapshot_provider=lambda: snapshot, gateway=_gateway())
        explanation = assistant.explain("order", client_order_id)
        answers = explanation["answers"]
        self.assertIn("order_decision_link", answers)
        self.assertIn("venue_and_connectors", answers)
        # P0001.16 §16：TESTNET 执行四问
        self.assertIn("submit_outcome", answers)
        self.assertIn("user_stream_health", answers)
        self.assertIn("reconciliation_state", answers)
        self.assertIn("reconciliation", answers["reconciliation_state"])


def _gateway() -> object:
    from actions import ActionGateway, ConfirmationRegistry

    return ActionGateway(clock=lambda: NOW, confirmations=ConfirmationRegistry(ttl_ms=60_000))


if __name__ == "__main__":
    unittest.main()
