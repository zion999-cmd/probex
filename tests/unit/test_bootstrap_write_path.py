"""P0001.9.7.1 切片 3：BOOTSTRAP authority 接入真实写路径（写边界）单测。

覆盖人类验收清单 1 – 14：NORMAL 无回归、BOOTSTRAP 第一笔可写、额度在网络调用前消耗、
timeout / CONFIRMED_REJECTED / REPLACE 都不能产生第二笔写、CANCEL 始终允许、
Mainnet / symbol mismatch / notional>100 / 非 PostOnly 拒绝、supersede 与升级。
"""

from __future__ import annotations

import unittest

from connectors.binance.execution import (
    BinanceExecutionAdapter,
    ExecutionAuthorityContext,
    SubmitClassification,
)
from connectors.binance.market_data.errors import TransportError
from connectors.binance.market_data.exchange_info import TradingRules
from execution.types import Order, OrderStatus
from market.events.types import Venue
from portfolio.types import Side
from readiness.authority import (
    AuthorityError,
    AuthorityInvalidReason,
    ExecutionReadinessAuthority,
    ReadinessProvenance,
    evidence_digest,
)
from readiness.bootstrap import (
    BootstrapAuthority,
    BootstrapAuthorityCoordinator,
    BootstrapEligibility,
    BootstrapPhase,
    BootstrapWriteGate,
)
from readiness.types import (
    Environment,
    LiveReadinessReason,
    LiveReadinessResult,
    LiveReadinessScope,
    LiveReadinessStatus,
    PrivateLatencyStatus,
    RecoveryGeneration,
)
from risk.types import KillSwitchMode
from tests.execution_support_live import FakeExecutionFetcher, ack_payload, client, echoing_ack
from tests.support import BASE_TS

NOW = BASE_TS + 1_000
TTL = 30_000
MIN_QUOTE_SIZE = 0.0007


def rules(**overrides: object) -> TradingRules:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "tick_size": 0.1,
        "min_price": 100.0,
        "max_price": 1_000_000.0,
        "step_size": 0.0001,
        "min_qty": 0.0001,
        "max_qty": 100.0,
        "min_notional": 50.0,
    }
    values.update(overrides)
    return TradingRules(**values)  # type: ignore[arg-type]


def order(**overrides: object) -> Order:
    values: dict[str, object] = {
        "client_order_id": "probex-s1-000001",
        "venue": Venue.BINANCE,
        "symbol": "BTCUSDT",
        "side": Side.BUY,
        "price": 82_890.0,
        "quantity": MIN_QUOTE_SIZE,
        "status": OrderStatus.PENDING_CREATE,
        "created_at": BASE_TS,
        "updated_at": BASE_TS,
        "post_only": True,
    }
    values.update(overrides)
    return Order(**values)  # type: ignore[arg-type]


def provenance() -> ReadinessProvenance:
    return ReadinessProvenance(
        recovery_generation=RecoveryGeneration(0, 1),
        market_generation=7,
        market_evidence_ts=NOW - 1_000,
        account_snapshot_ts=NOW - 1_500,
        clock_calibration_ts=NOW - 2_000,
        hwm_activation_id="act-1",
        hwm_generation=2,
        risk_policy_fingerprint="risk:abc",
        evidence_digest=evidence_digest({"k": "v"}),
    )


def eligibility() -> BootstrapEligibility:
    return BootstrapEligibility(
        recovery_status=__import__(
            "connectors.binance.private.recovery", fromlist=["RecoveryStatus"]
        ).RecoveryStatus.RECOVERED,
        market_ready=True,
        private_continuity_valid=True,
        account_flat=True,
        probex_open_orders=0,
        foreign_open_orders=0,
        uncertain_exposure_qty=0.0,
        latency_status=PrivateLatencyStatus.UNOBSERVED,
        environment=Environment.TESTNET,
    )


def unobserved() -> LiveReadinessResult:
    return LiveReadinessResult(
        status=LiveReadinessStatus.BOOTSTRAP_ELIGIBLE,
        scope=None,
        reasons=(LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED,),
        latency_status=PrivateLatencyStatus.UNOBSERVED,
    )


def normal_authority() -> ExecutionReadinessAuthority:
    from readiness.authority import issue_authority

    return issue_authority(
        LiveReadinessResult(
            status=LiveReadinessStatus.LIVE_READY,
            scope=LiveReadinessScope.TESTNET_LIVE_READY,
            reasons=(),
            latency_status=PrivateLatencyStatus.HEALTHY,
        ),
        provenance=provenance(),
        authority_id="normal-1",
        now_ms=NOW,
        authority_ttl_ms=TTL,
        environment=Environment.TESTNET,
    )


def bootstrap() -> tuple[BootstrapAuthorityCoordinator, BootstrapAuthority]:
    coord = BootstrapAuthorityCoordinator(symbol="BTCUSDT", bootstrap_ttl_ms=TTL, normal_ttl_ms=TTL)
    activation = coord.activate(result=unobserved(), eligibility=eligibility(), provenance=provenance(),
                                now_ms=NOW, environment=Environment.TESTNET)
    assert activation.phase is BootstrapPhase.ACTIVE
    authority = activation.authority
    assert isinstance(authority, BootstrapAuthority)
    return coord, authority


def context(*, authority, gate=None, latency=PrivateLatencyStatus.UNOBSERVED) -> ExecutionAuthorityContext:
    return ExecutionAuthorityContext(
        authority=authority,
        recovery_generation=RecoveryGeneration(0, 1),
        market_generation=7,
        hwm_activation_id="act-1",
        hwm_generation=2,
        kill_switch_mode=KillSwitchMode.NORMAL,
        bootstrap_gate=gate,
        latency_status=latency,
        private_continuity_valid=True,
    )


def build(*, fetcher=None, ctx=None, environment=Environment.TESTNET, symbol="BTCUSDT"):
    transport = fetcher if fetcher is not None else FakeExecutionFetcher(responses={"POST": ack_payload()})
    adapter = BinanceExecutionAdapter(
        rest=client(transport, now_ms=NOW),
        environment=environment,
        authority_provider=lambda: ctx,
        rules_provider=lambda: rules(),
        symbol=symbol,
    )
    return adapter, transport


class NormalPathRegressionTest(unittest.TestCase):
    def test_1_normal_authority_still_reaches_the_write_boundary(self) -> None:
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=normal_authority()))

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_ACCEPTED)
        self.assertEqual([method for method, _ in fetcher.calls], ["POST"])

    def test_1b_normal_authority_is_not_limited_by_bootstrap_quota(self) -> None:
        fetcher = FakeExecutionFetcher(responses={"POST": echoing_ack})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=normal_authority()))

        first = adapter.submit_with_outcome(order())
        second = adapter.submit_with_outcome(order(client_order_id="probex-s1-000002"))

        self.assertIs(first.classification, SubmitClassification.CONFIRMED_ACCEPTED)
        self.assertIs(second.classification, SubmitClassification.CONFIRMED_ACCEPTED)
        self.assertEqual(len(fetcher.calls), 2)


class BootstrapWriteBoundaryTest(unittest.TestCase):
    def test_2_first_bootstrap_write_reaches_the_boundary(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=BootstrapWriteGate(coord)))

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_ACCEPTED)
        self.assertEqual([method for method, _ in fetcher.calls], ["POST"])
        self.assertEqual(coord.write_attempts, 1)

    def test_3_quota_is_consumed_before_the_network_call(self) -> None:
        """在 fetcher 被调用的那一刻，额度必须**已经**被消耗。"""
        coord, authority = bootstrap()
        observed: list[int] = []

        def probe(url: str):
            observed.append(coord.write_attempts)
            return ack_payload()

        fetcher = FakeExecutionFetcher(responses={"POST": probe})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=BootstrapWriteGate(coord)))
        adapter.submit_with_outcome(order())

        self.assertEqual(observed, [1], "record_write_attempt must happen before the REST call")

    def test_4_transport_timeout_consumes_quota_and_blocks_second_write(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": TransportError("POST /fapi/v1/order failed: TimeoutError")})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=BootstrapWriteGate(coord)))

        first = adapter.submit_with_outcome(order())
        second = adapter.submit_with_outcome(order(client_order_id="probex-s1-000002"))

        self.assertIs(first.classification, SubmitClassification.UNKNOWN)
        self.assertEqual(coord.write_attempts, 1, "timeout must not restore the quota")
        self.assertIs(second.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn(AuthorityInvalidReason.ORDERS_USED_EXCEEDS_MAX.value, second.rejection_message)
        self.assertEqual(len(fetcher.calls), 1, "second write must never reach the exchange")

    def test_5_confirmed_rejection_still_consumes_quota(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=BootstrapWriteGate(coord)))

        first = adapter.submit_with_outcome(order())
        self.assertIs(first.classification, SubmitClassification.CONFIRMED_ACCEPTED)

        second = adapter.submit_with_outcome(order(client_order_id="probex-s1-000002"))
        self.assertIs(second.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn(AuthorityInvalidReason.ORDERS_USED_EXCEEDS_MAX.value, second.rejection_message)
        self.assertEqual(len(fetcher.calls), 1)

    def test_6_replace_cannot_produce_a_second_write(self) -> None:
        """REPLACE 必然需要新的写请求 ⇒ 必须被拒绝（额度已在第一笔消耗）。"""
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=BootstrapWriteGate(coord)))
        adapter.submit_with_outcome(order())

        replaced = adapter.submit_with_outcome(
            order(client_order_id="probex-s1-000002", price=82_800.0, quantity=MIN_QUOTE_SIZE)
        )
        self.assertIs(replaced.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertEqual(len(fetcher.calls), 1)

    def test_7_cancel_is_always_allowed(self) -> None:
        """额度耗尽后 CANCEL 仍然允许（去风险动作不经过 bootstrap 额度）。"""
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload(), "DELETE": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=BootstrapWriteGate(coord)))
        adapter.submit_with_outcome(order())
        self.assertEqual(coord.write_attempts, 1)

        events = adapter.cancel(order(status=OrderStatus.OPEN))

        self.assertIsInstance(events, tuple)   # 不抛错即视为允许；CANCEL 不经过 bootstrap 额度
        self.assertGreaterEqual(len(fetcher.calls), 1)
        self.assertEqual(coord.write_attempts, 1, "cancel must not consume the bootstrap quota")

    def test_8_mainnet_bootstrap_is_refused(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(
            fetcher=fetcher,
            ctx=context(authority=authority, gate=BootstrapWriteGate(coord)),
            environment=Environment.MAINNET,
        )

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn(AuthorityInvalidReason.ENVIRONMENT_NOT_TESTNET.value, outcome.rejection_message)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(coord.write_attempts, 0)

    def test_9_symbol_mismatch_is_refused(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=BootstrapWriteGate(coord)))

        outcome = adapter.submit_with_outcome(order(symbol="ETHUSDT"))

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(coord.write_attempts, 0)

    def test_9b_gate_rejects_symbol_mismatch(self) -> None:
        coord, authority = bootstrap()
        verdict = BootstrapWriteGate(coord).authorize(
            authority=authority, now_ms=NOW, requested_environment=Environment.TESTNET, symbol="ETHUSDT",
            notional_usdt=58.0, post_only=True, private_continuity_valid=True,
            latency_status=PrivateLatencyStatus.UNOBSERVED,
            recovery_generation=RecoveryGeneration(0, 1), market_generation=7,
            kill_switch_mode=KillSwitchMode.NORMAL,
        )
        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.SCOPE_MISMATCH, verdict.reasons)
        self.assertEqual(coord.write_attempts, 0)

    def test_10_notional_above_100_is_refused(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=BootstrapWriteGate(coord)))

        # 0.002 × 82 890 = 165.78 USDT > 100
        outcome = adapter.submit_with_outcome(order(quantity=0.002))

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn(AuthorityInvalidReason.NOTIONAL_EXCEEDS_MAX.value, outcome.rejection_message)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(coord.write_attempts, 0)

    def test_11_non_post_only_is_refused(self) -> None:
        coord, authority = bootstrap()
        verdict = BootstrapWriteGate(coord).authorize(
            authority=authority, now_ms=NOW, requested_environment=Environment.TESTNET, symbol="BTCUSDT",
            notional_usdt=58.0, post_only=False, private_continuity_valid=True,
            latency_status=PrivateLatencyStatus.UNOBSERVED,
            recovery_generation=RecoveryGeneration(0, 1), market_generation=7,
            kill_switch_mode=KillSwitchMode.NORMAL,
        )
        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.POST_ONLY_REQUIRED, verdict.reasons)
        self.assertEqual(coord.write_attempts, 0)

    def test_12_private_event_supersedes_bootstrap_at_the_write_boundary(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(
            fetcher=fetcher,
            ctx=context(authority=authority, gate=BootstrapWriteGate(coord),
                        latency=PrivateLatencyStatus.HEALTHY),
        )

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn(AuthorityInvalidReason.BOOTSTRAP_SUPERSEDED.value, outcome.rejection_message)
        self.assertEqual(fetcher.calls, [])

    def test_12b_missing_latency_status_is_treated_as_unknown(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        ctx = ExecutionAuthorityContext(
            authority=authority,
            recovery_generation=RecoveryGeneration(0, 1),
            market_generation=7,
            hwm_activation_id="act-1",
            hwm_generation=2,
            kill_switch_mode=KillSwitchMode.NORMAL,
            bootstrap_gate=BootstrapWriteGate(coord),
            latency_status=None,
            private_continuity_valid=True,
        )
        adapter, _ = build(fetcher=fetcher, ctx=ctx)

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn(AuthorityInvalidReason.BOOTSTRAP_SUPERSEDED.value, outcome.rejection_message)

    def test_missing_gate_is_refused(self) -> None:
        _, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=authority, gate=None))

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn("BOOTSTRAP_WRITE_GATE_MISSING", outcome.rejection_message)
        self.assertEqual(fetcher.calls, [])

    def test_13_healthy_latency_upgrades_to_normal_authority(self) -> None:
        coord, _ = bootstrap()
        coord.record_write_attempt()
        healthy = LiveReadinessResult(
            status=LiveReadinessStatus.LIVE_READY,
            scope=LiveReadinessScope.TESTNET_LIVE_READY,
            reasons=(),
            latency_status=PrivateLatencyStatus.HEALTHY,
        )
        activation = coord.on_private_latency_event(result=healthy, provenance=provenance(),
                                                    now_ms=NOW + 1_000, environment=Environment.TESTNET)

        self.assertIs(activation.phase, BootstrapPhase.NORMAL_READY)
        normal = activation.authority
        self.assertIsInstance(normal, ExecutionReadinessAuthority)

        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(fetcher=fetcher, ctx=context(authority=normal, gate=None,
                                                        latency=PrivateLatencyStatus.HEALTHY))
        outcome = adapter.submit_with_outcome(order())
        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_ACCEPTED)

    def test_14_unhealthy_or_unknown_after_private_event_is_blocked(self) -> None:
        for latency, reason in ((PrivateLatencyStatus.UNHEALTHY, LiveReadinessReason.PRIVATE_LATENCY_TOO_HIGH),
                                (PrivateLatencyStatus.UNKNOWN, LiveReadinessReason.PRIVATE_LATENCY_UNKNOWN)):
            with self.subTest(latency=latency.value):
                coord, _ = bootstrap()
                blocked = LiveReadinessResult(
                    status=LiveReadinessStatus.BLOCKED, scope=None, reasons=(reason,), latency_status=latency
                )
                activation = coord.on_private_latency_event(
                    result=blocked, provenance=provenance(), now_ms=NOW + 1_000,
                    environment=Environment.TESTNET)
                self.assertIs(activation.phase, BootstrapPhase.BLOCKED)
                self.assertIsNone(activation.authority)
                self.assertIsNone(coord.authority)


class BootstrapJitContractTest(unittest.TestCase):
    """JIT 契约（P0001.9.7.1 §一/JIT）：授权必须临近写入签发，且事实一变即失效。"""

    def test_expired_authority_is_refused_without_network(self) -> None:
        """提前签发后长时间等待 PLACE ⇒ 过期 ⇒ 本地拒绝、零网络调用、零额度消耗。"""
        coord, _ = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        late = NOW + TTL + 1
        adapter = BinanceExecutionAdapter(
            rest=client(fetcher, now_ms=late),
            environment=Environment.TESTNET,
            authority_provider=lambda: context(authority=coord.bootstrap_authority,
                                              gate=BootstrapWriteGate(coord)),
            rules_provider=lambda: rules(),
            symbol="BTCUSDT",
        )

        outcome = adapter.submit_with_outcome(order())

        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn(AuthorityInvalidReason.AUTHORITY_EXPIRED.value, outcome.rejection_message)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(coord.write_attempts, 0)

    def test_reissue_in_the_same_round_is_allowed_and_supersedes_the_old_one(self) -> None:
        """PLACE 出现当轮重新 collect + 重新 issue（新 generation）⇒ 可通过并写入。"""
        coord, first = bootstrap()
        second_activation = coord.activate(
            result=unobserved(), eligibility=eligibility(),
            provenance=ReadinessProvenance(
                recovery_generation=RecoveryGeneration(0, 2),
                market_generation=8,
                market_evidence_ts=NOW + 600_000,
                account_snapshot_ts=NOW + 600_000,
                clock_calibration_ts=NOW + 600_000,
                hwm_activation_id="act-1",
                hwm_generation=2,
                risk_policy_fingerprint="risk:abc",
                evidence_digest=evidence_digest({"k": "v2"}),
            ),
            now_ms=NOW + 600_000, environment=Environment.TESTNET,
        )
        self.assertIs(second_activation.phase, BootstrapPhase.ACTIVE)
        second = second_activation.authority
        assert isinstance(second, BootstrapAuthority)
        self.assertNotEqual(second.authority_id, first.authority_id)
        self.assertEqual(second.bootstrap_generation, 2)

        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter = BinanceExecutionAdapter(
            rest=client(fetcher, now_ms=NOW + 600_001),
            environment=Environment.TESTNET,
            authority_provider=lambda: ExecutionAuthorityContext(
                authority=second, recovery_generation=RecoveryGeneration(0, 2), market_generation=8,
                hwm_activation_id="act-1", hwm_generation=2, kill_switch_mode=KillSwitchMode.NORMAL,
                bootstrap_gate=BootstrapWriteGate(coord), latency_status=PrivateLatencyStatus.UNOBSERVED,
                private_continuity_valid=True,
            ),
            rules_provider=lambda: rules(),
            symbol="BTCUSDT",
        )
        outcome = adapter.submit_with_outcome(order())
        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_ACCEPTED)
        self.assertEqual(coord.write_attempts, 1)

    def test_reissue_after_quota_consumed_is_refused(self) -> None:
        """额度已按 write attempt 消耗 ⇒ 再次 activate（想拿第二笔）必须被拒。"""
        coord, _ = bootstrap()
        coord.record_write_attempt()
        again = coord.activate(result=unobserved(), eligibility=eligibility(), provenance=provenance(),
                               now_ms=NOW + 1_000, environment=Environment.TESTNET)
        self.assertIs(again.phase, BootstrapPhase.BLOCKED)
        self.assertIsNone(again.authority)
        self.assertIn(AuthorityInvalidReason.ORDERS_USED_EXCEEDS_MAX.value, again.reasons)

    def test_continuity_lost_between_collect_and_write_is_refused(self) -> None:
        coord, authority = bootstrap()
        fetcher = FakeExecutionFetcher(responses={"POST": ack_payload()})
        adapter, _ = build(
            fetcher=fetcher,
            ctx=ExecutionAuthorityContext(
                authority=authority, recovery_generation=RecoveryGeneration(0, 1), market_generation=7,
                hwm_activation_id="act-1", hwm_generation=2, kill_switch_mode=KillSwitchMode.NORMAL,
                bootstrap_gate=BootstrapWriteGate(coord), latency_status=PrivateLatencyStatus.UNOBSERVED,
                private_continuity_valid=False,
            ),
        )
        outcome = adapter.submit_with_outcome(order())
        self.assertIs(outcome.classification, SubmitClassification.CONFIRMED_REJECTED)
        self.assertIn(AuthorityInvalidReason.PRIVATE_CONTINUITY_INVALID.value, outcome.rejection_message)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(coord.write_attempts, 0)

    def test_recovery_generation_changed_between_collect_and_write_is_refused(self) -> None:
        """WS reconnect 后 recovery 重新跑（generation 改变）⇒ 旧 authority 不得沿用。"""
        coord, authority = bootstrap()
        verdict = BootstrapWriteGate(coord).check(
            authority=authority, now_ms=NOW, requested_environment=Environment.TESTNET, symbol="BTCUSDT",
            notional_usdt=58.0, post_only=True, private_continuity_valid=True,
            latency_status=PrivateLatencyStatus.UNOBSERVED,
            recovery_generation=RecoveryGeneration(1, 1),   # reconnect 后的新 generation
            market_generation=7, kill_switch_mode=KillSwitchMode.NORMAL,
        )
        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.RECOVERY_GENERATION_CHANGED, verdict.reasons)


class BootstrapAuthorityCannotBeForgedTest(unittest.TestCase):
    def test_bootstrap_authority_requires_eligible_readiness(self) -> None:
        with self.assertRaises(AuthorityError):
            BootstrapAuthority(
                authority_id="x", bootstrap_generation=1, issued_at_ms=NOW, expires_at_ms=NOW + TTL,
                environment=Environment.TESTNET, symbol="BTCUSDT", max_orders=1, post_only_required=True,
                max_notional_usdt=100.0, recovery_generation=RecoveryGeneration(0, 1), market_generation=1,
                market_evidence_ts=NOW, evidence_digest="sha256:x",
                readiness_status=LiveReadinessStatus.LIVE_READY,
            )

    def test_gate_requires_a_bootstrap_authority(self) -> None:
        coord, _ = bootstrap()
        with self.assertRaises(AuthorityError):
            BootstrapWriteGate(coord).authorize(
                authority=normal_authority(), now_ms=NOW, requested_environment=Environment.TESTNET,
                symbol="BTCUSDT", notional_usdt=10.0, post_only=True, private_continuity_valid=True,
                latency_status=PrivateLatencyStatus.UNOBSERVED,
                recovery_generation=RecoveryGeneration(0, 1), market_generation=7,
                kill_switch_mode=KillSwitchMode.NORMAL,
            )


if __name__ == "__main__":
    unittest.main()
