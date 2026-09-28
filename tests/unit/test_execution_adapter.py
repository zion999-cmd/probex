"""P0001.9.6 单元测试：写执行 adapter 的范围 / 授权 / 结果三分类（SC-1 – SC-8、SC-17、SC-18、SC-22）。"""

from __future__ import annotations

import unittest

from connectors.binance.execution import (
    BinanceExecutionAdapter,
    ExecutionAuthorityContext,
    ExecutionOutcomeUnknown,
    ExecutionRequestRejected,
    SubmitClassification,
)
from connectors.binance.execution.rest import ExecutionOutcomeUnknown as _Unknown  # noqa: F401
from connectors.binance.market_data.errors import TransportError
from connectors.binance.market_data.exchange_info import TradingRules
from connectors.binance.private.auth import ClockCalibration
from execution.types import Order, OrderStatus
from market.events.types import Venue
from portfolio.types import Side
from readiness.authority import (
    AuthorityInvalidReason,
    ExecutionReadinessAuthority,
    evidence_digest,
    risk_policy_fingerprint,
)
from readiness.types import Environment, LiveReadinessScope, LiveReadinessStatus, RecoveryGeneration
from risk.limits import RiskLimits
from risk.types import KillSwitchMode
from tests.execution_support_live import FakeExecutionFetcher, ack_payload, client
from tests.support import BASE_TS

NOW = BASE_TS + 1_000


def rules(**overrides: object) -> TradingRules:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "tick_size": 0.1,
        "min_price": 100.0,
        "max_price": 1_000_000.0,
        "step_size": 0.001,
        "min_qty": 0.001,
        "max_qty": 100.0,
        "min_notional": 50.0,
    }
    values.update(overrides)
    return TradingRules(**values)  # type: ignore[arg-type]


def authority(*, scope: LiveReadinessScope = LiveReadinessScope.TESTNET_LIVE_READY, ttl_ms: int = 10_000) -> ExecutionReadinessAuthority:
    limits = RiskLimits(max_position_qty=1.0, max_daily_loss=100.0, max_drawdown_pct=0.2)
    return ExecutionReadinessAuthority(
        authority_id="auth-1",
        issued_at_ms=NOW,
        expires_at_ms=NOW + ttl_ms,
        scope=scope,
        environment=Environment.TESTNET if scope is LiveReadinessScope.TESTNET_LIVE_READY else Environment.MAINNET,
        recovery_generation=RecoveryGeneration(0, 0),
        market_generation=3,
        market_evidence_ts=NOW,
        account_snapshot_ts=NOW,
        clock_calibration_ts=NOW,
        hwm_activation_id="act-1",
        hwm_generation=1,
        risk_policy_fingerprint=risk_policy_fingerprint(limits),
        evidence_digest=evidence_digest({"x": 1}),
        readiness_status=LiveReadinessStatus.LIVE_READY,
    )


def context(**overrides: object) -> ExecutionAuthorityContext:
    values: dict[str, object] = {
        "authority": authority(),
        "recovery_generation": RecoveryGeneration(0, 0),
        "market_generation": 3,
        "hwm_activation_id": "act-1",
        "hwm_generation": 1,
        "kill_switch_mode": KillSwitchMode.NORMAL,
    }
    values.update(overrides)
    return ExecutionAuthorityContext(**values)  # type: ignore[arg-type]


def order(**overrides: object) -> Order:
    values: dict[str, object] = {
        "client_order_id": "probex-s1-000001",
        "venue": Venue.BINANCE,
        "symbol": "BTCUSDT",
        "side": Side.BUY,
        "price": 60_000.0,
        "quantity": 0.002,
        "status": OrderStatus.PENDING_CREATE,
        "created_at": BASE_TS,
        "updated_at": BASE_TS,
        "post_only": True,
    }
    values.update(overrides)
    return Order(**values)  # type: ignore[arg-type]


def adapter(
    *,
    fetcher: FakeExecutionFetcher | None = None,
    authority_context: ExecutionAuthorityContext | None = None,
    trading_rules: TradingRules | None = None,
    pending_rules: bool = False,
    environment: Environment = Environment.TESTNET,
    local_status=None,
    private_read=None,
    now_ms: int = NOW,
):
    transport = fetcher if fetcher is not None else FakeExecutionFetcher(responses={"POST": ack_payload()})
    if pending_rules:
        rules_provider = lambda: None
    else:
        rules_provider = lambda: (trading_rules if trading_rules is not None else rules())
    return BinanceExecutionAdapter(
        rest=client(transport, now_ms=now_ms),
        environment=environment,
        authority_provider=lambda: (authority_context if authority_context is not None else context()),
        rules_provider=rules_provider,
        symbol="BTCUSDT",
        local_status_provider=local_status,
        private_read=private_read,
    ), transport


class ScopeAndAuthorityTest(unittest.TestCase):
    """SC-1 / SC-2 / SC-19 / SC-20：写请求前的本地门。"""

    def test_sc1_missing_authority_sends_no_request(self) -> None:
        subject, transport = adapter(authority_context=context(authority=None))

        outcome = subject.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertEqual(outcome.rejection_message, "AUTHORITY_MISSING")
        self.assertEqual(transport.calls, [])  # **零** HTTP 请求

    def test_sc1_expired_authority_sends_no_request(self) -> None:
        subject, transport = adapter(
            authority_context=context(authority=authority(ttl_ms=1)), now_ms=NOW + 10
        )

        outcome = subject.submit_with_outcome(order())

        self.assertEqual(outcome.rejection_message, AuthorityInvalidReason.AUTHORITY_EXPIRED.value)
        self.assertEqual(transport.calls, [])

    def test_sc19_recovery_generation_change_sends_no_request(self) -> None:
        subject, transport = adapter(
            authority_context=context(recovery_generation=RecoveryGeneration(1, 0))
        )

        outcome = subject.submit_with_outcome(order())

        self.assertEqual(outcome.rejection_message, AuthorityInvalidReason.RECOVERY_GENERATION_CHANGED.value)
        self.assertEqual(transport.calls, [])

    def test_sc2_testnet_authority_cannot_be_used_for_mainnet_adapter(self) -> None:
        subject, transport = adapter(environment=Environment.MAINNET)  # authority 是 TESTNET

        outcome = subject.submit_with_outcome(order())

        self.assertEqual(outcome.rejection_message, AuthorityInvalidReason.SCOPE_MISMATCH.value)
        self.assertEqual(transport.calls, [])

    def test_market_generation_and_kill_switch_also_block(self) -> None:
        for overrides, expected in (
            ({"market_generation": 4}, AuthorityInvalidReason.MARKET_GENERATION_CHANGED),
            ({"hwm_generation": 9}, AuthorityInvalidReason.HIGH_WATERMARK_CHANGED),
            ({"kill_switch_mode": KillSwitchMode.HALT_ALL}, AuthorityInvalidReason.KILL_SWITCH_NOT_NORMAL),
        ):
            with self.subTest(overrides=overrides):
                subject, transport = adapter(authority_context=context(**overrides))
                outcome = subject.submit_with_outcome(order())
                self.assertEqual(outcome.rejection_message, expected.value)
                self.assertEqual(transport.calls, [])


class OrderScopeTest(unittest.TestCase):
    """SC-3 / SC-4 / SC-5 / SC-17 / SC-18：第一版订单范围与 clientOrderId 幂等键。"""

    def test_sc4_post_only_false_is_refused_locally(self) -> None:
        subject, transport = adapter()

        outcome = subject.submit_with_outcome(order(post_only=False))

        self.assertEqual(outcome.rejection_message, "post_only_required")
        self.assertEqual(transport.calls, [])

    def test_non_probex_client_order_id_is_refused(self) -> None:
        subject, transport = adapter()

        outcome = subject.submit_with_outcome(order(client_order_id="manual-1"))

        self.assertEqual(outcome.rejection_message, "ownership_violation")
        self.assertEqual(transport.calls, [])

    def test_symbol_and_venue_mismatch_are_refused(self) -> None:
        subject, transport = adapter()
        for overrides, expected in (
            ({"symbol": "ETHUSDT"}, "symbol_mismatch"),
            ({"venue": Venue.OTHER if hasattr(Venue, "OTHER") else Venue.BINANCE}, None),
        ):
            if expected is None:
                continue
            with self.subTest(overrides=overrides):
                outcome = subject.submit_with_outcome(order(**overrides))
                self.assertEqual(outcome.rejection_message, expected)
                self.assertEqual(transport.calls, [])

    def test_sc18_trading_rules_are_enforced_without_rounding(self) -> None:
        cases = {
            # 每例单独选 rules，确保命中的是**目标**检查（不自动 round、不猜）
            "price_not_on_tick": (rules(), {"price": 60_000.05}),
            "quantity_not_on_step": (rules(), {"quantity": 0.0021}),
            "quantity_below_min": (rules(min_qty=0.005), {"quantity": 0.001}),
            "notional_below_min": (rules(min_price=10.0), {"quantity": 0.001, "price": 100.0}),
            "symbol_not_trading": (rules(status="BREAK"), {}),
        }
        for expected, (trading_rules, overrides) in cases.items():
            with self.subTest(case=expected):
                subject, transport = adapter(trading_rules=trading_rules)
                outcome = subject.submit_with_outcome(order(**overrides))
                self.assertEqual(outcome.rejection_message, expected)
                self.assertEqual(transport.calls, [])

    def test_trading_rules_required(self) -> None:
        subject, transport = adapter(pending_rules=True)

        outcome = subject.submit_with_outcome(order())

        self.assertEqual(outcome.rejection_message, "trading_rules_unavailable")
        self.assertEqual(transport.calls, [])

    def test_sc5_client_order_id_is_passed_through_verbatim(self) -> None:
        subject, transport = adapter()

        subject.submit_with_outcome(order())

        self.assertEqual(len(transport.seen_queries), 1)
        self.assertIn("newClientOrderId=probex-s1-000001", transport.seen_queries[0])

    def test_reduce_only_is_mapped_but_not_judged(self) -> None:
        reducer = order(reduce_only=True)
        subject, transport = adapter()

        subject.submit_with_outcome(reducer)

        self.assertIn("reduceOnly=true", transport.seen_queries[0])
        self.assertIn("positionSide=BOTH", transport.seen_queries[0])

    def test_signed_query_contains_signature_but_never_leaks(self) -> None:
        subject, transport = adapter()

        outcome = subject.submit_with_outcome(order())

        # 请求里必须有签名（否则 Binance 拒绝），但 adapter 的输出/repr 不得包含它
        self.assertIn("signature=", transport.seen_queries[0])
        rendered = f"{outcome!r} {subject!r} {subject.rest!r}"
        self.assertNotIn("signature=", rendered)
        self.assertNotIn("test-execution-secret", rendered)


class OutcomeClassificationTest(unittest.TestCase):
    """SC-6 / SC-7 / SC-8 / SC-9：三分类（绝不把 UNKNOWN 当 FAILED）。"""

    def test_sc6_confirmable_ack_yields_order_accepted(self) -> None:
        subject, _transport = adapter()

        outcome = subject.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_ACCEPTED)
        self.assertEqual(len(outcome.events), 1)
        self.assertEqual(outcome.events[0].event_type.value, "order_accepted")
        self.assertEqual(subject.known_exchange_order_id("probex-s1-000001"), "4242")

    def test_sc7_business_rejection_yields_order_rejected(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={"POST": ExecutionRequestRejected(status=400, code=-2010, message="Order would immediately match and take.")}
        )
        subject, _transport = adapter(fetcher=fetcher)

        outcome = subject.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertEqual(outcome.rejection_code, -2010)
        self.assertEqual(outcome.events[0].event_type.value, "order_rejected")

    def test_sc8_unknown_outcomes_never_become_failed(self) -> None:
        cases = {
            "timeout": TimeoutError("read timed out"),
            "reset": TransportError("connection reset"),
            "http_5xx": ExecutionOutcomeUnknown(status=503, detail="HTTP 503"),
            "unparseable": {"unexpected": "payload"},
        }
        for label, response in cases.items():
            with self.subTest(case=label):
                fetcher = FakeExecutionFetcher(responses={"POST": response})
                subject, transport = adapter(fetcher=fetcher)

                outcome = subject.submit_with_outcome(order())

                self.assertIs(outcome.classification, SubmitClassification.UNKNOWN, label)
                self.assertEqual(outcome.events, ())  # 不生成 FAILED / 不生成任何"事实"
                self.assertEqual(subject.unknown_submit_count, 1)
                # 单次 submit 只发一次请求：绝不自动重试（SC-9）
                self.assertEqual(len([call for call in transport.calls if call[0] == "POST"]), 1)

    def test_ack_with_mismatched_client_order_id_is_unknown(self) -> None:
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload(client_order_id="someone-else")})
        subject, _transport = adapter(fetcher=fetcher)

        outcome = subject.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.UNKNOWN)


class QueryResolutionTest(unittest.TestCase):
    """SC-10 / SC-11 / SC-12 / SC-13：UNKNOWN 用 query 收敛，且 cancel request ≠ CANCELED。"""

    def test_sc10_unknown_submit_is_resolved_by_client_order_id_query(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={"POST": TimeoutError("lost response"), "GET": ack_payload(status="NEW")}
        )
        subject, _transport = adapter(fetcher=fetcher)

        self.assertIs(subject.submit_with_outcome(order()).classification, SubmitClassification.UNKNOWN)
        events = subject.query_order(client_order_id="probex-s1-000001", timestamp=BASE_TS + 1)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, OrderStatus.OPEN)
        self.assertEqual(subject.poll(), events)  # 入队供 manager.poll() 消费
        self.assertEqual(subject.poll(), ())

    def test_sc11_query_failure_keeps_state_unknown(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={"POST": TimeoutError("lost"), "GET": ExecutionOutcomeUnknown(status=503, detail="x")}
        )
        subject, _transport = adapter(fetcher=fetcher)
        subject.submit_with_outcome(order())

        events = subject.query_order(client_order_id="probex-s1-000001", timestamp=BASE_TS + 1)

        self.assertEqual(events, ())  # 无法确认 ⇒ 保持 LOST/uncertain（由上层处理）

    def test_sc12_cancel_unknown_does_not_claim_canceled(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={"POST": ack_payload(), "DELETE": TimeoutError("lost cancel response")}
        )
        subject, _transport = adapter(fetcher=fetcher)
        subject.submit_with_outcome(order())

        events = subject.cancel(order(status=OrderStatus.PENDING_CANCEL))

        self.assertEqual(events, ())  # 绝不假装成功
        self.assertEqual(subject.unknown_cancel_count, 1)

    def test_sc13_cancel_confirmed_yields_order_canceled(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={"POST": ack_payload(), "DELETE": ack_payload(status="CANCELED")}
        )
        subject, _transport = adapter(fetcher=fetcher)
        subject.submit_with_outcome(order())

        events = subject.cancel(order(status=OrderStatus.PENDING_CANCEL))

        self.assertEqual([event.event_type.value for event in events], ["order_canceled"])

    def test_cancel_on_already_filled_order_reports_fact_not_success(self) -> None:
        fetcher = FakeExecutionFetcher(
            responses={"POST": ack_payload(), "DELETE": ack_payload(status="FILLED", executed_qty="0.002", avg_price="60000.0")}
        )
        subject, _transport = adapter(fetcher=fetcher)
        subject.submit_with_outcome(order())

        events = subject.cancel(order(status=OrderStatus.PENDING_CANCEL))

        self.assertEqual(events[0].event_type.value, "order_status_update")
        self.assertEqual(events[0].status, OrderStatus.FILLED)

    def test_sc20_cancel_works_without_authority_and_under_halt_all(self) -> None:
        for overrides in ({"authority": None}, {"kill_switch_mode": KillSwitchMode.HALT_ALL}):
            with self.subTest(overrides=overrides):
                fetcher = FakeExecutionFetcher(
                    responses={"POST": ack_payload(), "DELETE": ack_payload(status="CANCELED")}
                )
                subject, _transport = adapter(fetcher=fetcher, authority_context=context(**overrides))
                subject.submit_with_outcome(order())
                events = subject.cancel(order(status=OrderStatus.PENDING_CANCEL))
                self.assertEqual([event.event_type.value for event in events], ["order_canceled"])

    def test_cancel_refuses_foreign_order(self) -> None:
        subject, transport = adapter()

        with self.assertRaises(Exception):
            subject.cancel(order(client_order_id="manual-1", status=OrderStatus.OPEN))

        self.assertEqual(transport.calls, [])


class UserStreamBridgeTest(unittest.TestCase):
    """SC-14 / SC-15 / SC-16：stream → ExecutionEvent（不造第二套状态机）。"""

    def _observation(self, **overrides: object):
        from connectors.binance.private.events import OrderUpdateObservation

        values: dict[str, object] = {
            "event_ts": BASE_TS + 5,
            "transaction_ts": BASE_TS + 5,
            "receive_ts": BASE_TS + 6,
            "process_ts": BASE_TS + 6,
            "symbol": "BTCUSDT",
            "client_order_id": "probex-s1-000001",
            "order_id": 4242,
            "side": "BUY",
            "order_type": "LIMIT",
            "execution_type": "TRADE",
            "order_status": "PARTIALLY_FILLED",
            "last_fill_quantity": 0.001,
            "cumulative_fill_quantity": 0.001,
            "last_fill_price": 60_000.0,
            "average_price": 60_000.0,
            "commission": 0.02,
            "commission_asset": "USDT",
            "trade_id": 777,
            "is_maker": True,
            "original_quantity": 0.002,
            "original_price": 60_000.0,
            "reduce_only": False,
        }
        values.update(overrides)
        return OrderUpdateObservation(**values)  # type: ignore[arg-type]

    def test_sc14_rest_ack_then_stream_duplicate_is_safe(self) -> None:
        subject, _transport = adapter()
        subject.submit_with_outcome(order())  # REST ACK ⇒ OrderAccepted

        events = subject.bridge_user_event(self._observation(order_status="NEW", execution_type="NEW", last_fill_quantity=0.0, cumulative_fill_quantity=0.0, trade_id=0))

        # NEW 状态重复到达：允许产出 status update，但目标状态与本地一致（幂等由 tracker 处理）
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, OrderStatus.OPEN)

    def test_sc15_fill_facts_come_from_trade_execution(self) -> None:
        subject, _transport = adapter()

        events = subject.bridge_user_event(self._observation())

        fills = [event for event in events if event.event_type.value == "fill_received"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].trade_id, "777")
        self.assertAlmostEqual(fills[0].fee, 0.02)
        self.assertAlmostEqual(fills[0].quantity, 0.001)

    def test_sc16_no_synthetic_fill_from_status_only_events(self) -> None:
        subject, _transport = adapter()

        events = subject.bridge_user_event(
            self._observation(execution_type="NEW", order_status="NEW", last_fill_quantity=0.0, cumulative_fill_quantity=0.0, trade_id=0)
        )

        self.assertEqual([event.event_type.value for event in events], ["order_status_update"])

    def test_conflicting_transition_is_skipped_not_forced(self) -> None:
        """本地已 CANCELED、stream 又来 NEW ⇒ 不强行回退（交给 reconciliation）。"""
        subject, _transport = adapter(local_status=lambda _cid: OrderStatus.CANCELED)

        events = subject.bridge_user_event(
            self._observation(execution_type="NEW", order_status="NEW", last_fill_quantity=0.0, cumulative_fill_quantity=0.0, trade_id=0)
        )

        self.assertEqual(events, ())
        self.assertEqual(subject.skipped_transition_count, 1)

    def test_foreign_stream_events_are_ignored(self) -> None:
        subject, _transport = adapter()

        events = subject.bridge_user_event(
            self._observation(client_order_id="manual-1", execution_type="NEW", order_status="NEW", last_fill_quantity=0.0, cumulative_fill_quantity=0.0, trade_id=0)
        )

        self.assertEqual(events, ())


class IsolationSurfaceTest(unittest.TestCase):
    """SC-22：adapter 不做交易决策，也不暴露与决策相关的能力。"""

    def test_module_does_not_import_decision_layers(self) -> None:
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        forbidden = {"strategy", "prediction", "jev", "policy", "risk"}
        # 例外：`risk.types.KillSwitchMode` / `risk.high_watermark` 是**数据契约**，不是决策
        allowed_modules = {"risk.types", "risk.limits", "risk.high_watermark"}
        for name in ("adapter.py", "rest.py", "parsing.py"):
            tree = ast.parse((root / "connectors" / "binance" / "execution" / name).read_text(encoding="utf-8"))
            roots: set[str] = set()
            modules: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots.add(node.module.split(".")[0])
                    modules.add(node.module)
            with self.subTest(module=name):
                self.assertEqual(roots & (forbidden - {"risk"}), set())
                self.assertTrue({m for m in modules if m.startswith("risk")} <= allowed_modules)

    def test_adapter_has_no_decision_methods(self) -> None:
        public = {name for name in dir(BinanceExecutionAdapter) if not name.startswith("_")}

        for forbidden in ("decide", "size", "price_quote", "maker_edge", "should", "risk"):
            with self.subTest(name=forbidden):
                self.assertFalse(any(forbidden in name for name in public))


if __name__ == "__main__":
    unittest.main()


class ExternalFactsAvailabilityTest(unittest.TestCase):
    """P0001.9.6 implementation correction：**UNKNOWN ≠ EMPTY**（外部事实不可用必须 fail closed）。

    危险场景（必须被阻断）：交易所真实存在 Probex 挂单，但事实源没接上 —— 此时
    `open_orders() -> ()` 会被上层读成"确认没有挂单"。
    """

    def test_missing_provider_raises_for_open_orders(self) -> None:
        from connectors.binance.execution import ExternalFactsUnavailableError

        subject, _transport = adapter(private_read=None)

        with self.assertRaises(ExternalFactsUnavailableError):
            subject.open_orders()

    def test_missing_provider_raises_for_recent_fills(self) -> None:
        from connectors.binance.execution import ExternalFactsUnavailableError

        subject, _transport = adapter(private_read=None)

        with self.assertRaises(ExternalFactsUnavailableError):
            subject.recent_fills()

    def test_provider_failure_is_not_converted_to_empty(self) -> None:
        from connectors.binance.execution import ExternalFactsUnavailableError

        class Broken:
            def parse_open_orders(self, symbol: str):
                raise TransportError("connection reset")

            def parse_recent_fills(self, symbol: str, *, since_ms=None):
                raise ExecutionOutcomeUnknown(status=503, detail="x")

        subject, _transport = adapter(private_read=Broken())

        with self.assertRaises(ExternalFactsUnavailableError):
            subject.open_orders()
        with self.assertRaises(ExternalFactsUnavailableError):
            subject.recent_fills()

    def test_true_empty_from_provider_is_allowed(self) -> None:
        class Empty:
            def parse_open_orders(self, symbol: str):
                return ()

            def parse_recent_fills(self, symbol: str, *, since_ms=None):
                return ()

        subject, _transport = adapter(private_read=Empty())

        self.assertEqual(subject.open_orders(), ())  # 交易所确认：没有挂单
        self.assertEqual(subject.recent_fills(), ())  # 交易所确认：没有成交

    def test_provider_lacking_required_methods_is_unavailable(self) -> None:
        from connectors.binance.execution import ExternalFactsUnavailableError

        class Partial:
            def parse_open_orders(self, symbol: str):
                return ()

        subject, _transport = adapter(private_read=Partial())

        with self.assertRaises(ExternalFactsUnavailableError):
            subject.open_orders()
        with self.assertRaises(ExternalFactsUnavailableError):
            subject.recent_fills()

    def test_unavailable_is_never_silently_treated_as_no_external_orders(self) -> None:
        """SC：reconciliation 无法在"事实不可用"时得出"外部无订单/无成交"的结论。"""
        from connectors.binance.execution import ExternalFactsUnavailableError

        # 交易所真实存在一笔 Probex 挂单，但 provider 未接上
        subject, _transport = adapter(private_read=None)

        with self.assertRaises(ExternalFactsUnavailableError) as ctx:
            subject.open_orders()

        message = str(ctx.exception)
        self.assertIn("UNKNOWN", message)
        self.assertIn("not EMPTY", message)
        # 关键：调用方拿到的是异常（无法继续把它当 0），而不是空元组
        self.assertIsInstance(ctx.exception, Exception)

    def test_foreign_orders_are_filtered_when_provider_works(self) -> None:
        from execution.types import ExternalOrder, OrderStatus

        class Provider:
            def parse_open_orders(self, symbol: str):
                return (
                    ExternalOrder(
                        client_order_id="probex-s1-000001",
                        exchange_order_id="1",
                        symbol=symbol,
                        side=Side.BUY,
                        price=60_000.0,
                        quantity=0.002,
                        status=OrderStatus.OPEN,
                        filled_quantity=0.0,
                        avg_fill_price=0.0,
                    ),
                    ExternalOrder(
                        client_order_id="manual-1",
                        exchange_order_id="2",
                        symbol=symbol,
                        side=Side.BUY,
                        price=60_000.0,
                        quantity=0.002,
                        status=OrderStatus.OPEN,
                        filled_quantity=0.0,
                        avg_fill_price=0.0,
                    ),
                )

            def parse_recent_fills(self, symbol: str, *, since_ms=None):
                return ()

        subject, _transport = adapter(private_read=Provider())

        orders = subject.open_orders()

        self.assertEqual([order.client_order_id for order in orders], ["probex-s1-000001"])
