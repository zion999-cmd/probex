"""P0001.10.2 RunSummary builder 单测（SC-8 / SC-10）。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports import build_run_summary, summary_to_json, summary_to_markdown


def identity(mode: RuntimeMode = RuntimeMode.TESTNET) -> RuntimeIdentity:
    return RuntimeIdentity(mode=mode, environment="testnet", venue="binance", symbol="BTCUSDT",
                           runtime_id="rt-1", started_at=1, data_timestamp=Fact.of(2))


def telemetry(*, submit: int = 0, cancel: int = 0, replace: int = 0, risk_reject: int = 0,
              unknown_submit: int = 0, ts: int = 1_000, healthy: bool = True,
              prediction_id: str | None = "p1", authority_valid: bool = True,
              authority_reason: str | None = None) -> object:
    return SimpleNamespace(loop_ts=ts, market_healthy=healthy, prediction_id=prediction_id,
                           prediction_age_ms=700, submit_count=submit, cancel_count=cancel,
                           replace_count=replace, risk_reject_count=risk_reject,
                           unknown_submit_count=unknown_submit, authority_valid=authority_valid,
                           authority_reason=authority_reason)


def summary(**overrides: object):
    values: dict[str, object] = {
        "identity": identity(),
        "run_id": "run-1",
        "started_at": 1_000,
        "telemetry": (telemetry(submit=1, cancel=1, ts=1_000), telemetry(cancel=2, risk_reject=1, ts=4_000)),
        "orders": (SimpleNamespace(status=SimpleNamespace(value="CANCELED")),
                   SimpleNamespace(status="OPEN")),
        "fills": 1,
        "fees": 0.02,
        "realized_pnl": -0.5,
        "unrealized_pnl": 0.0,
        "max_exposure": 80.0,
        "final_position": 0.0,
        "readiness_blocks": ("PRIVATE_LATENCY_UNOBSERVED",),
        "config_fingerprints": {"risk_policy": "sha256:abc", "feature_schema": "market-state-v1"},
        "ended_at": 9_000,
        "data_range": (100, 900),
    }
    values.update(overrides)
    return build_run_summary(**values)  # type: ignore[arg-type]


class ReportBuilderTest(unittest.TestCase):
    def test_run_identity_binds_mode_symbol_and_fingerprints(self) -> None:
        """§5：报告必须绑定 run identity，不能只给一个孤立 PnL。"""
        built = summary()
        self.assertEqual(built.run.run_id, "run-1")
        self.assertEqual(built.run.runtime.mode, RuntimeMode.TESTNET)
        self.assertEqual(built.run.runtime.symbol, "BTCUSDT")
        self.assertEqual(built.run.started_at, 1_000)
        self.assertEqual(built.run.ended_at.value, 9_000)
        self.assertEqual(set(built.run.config_fingerprints), {"risk_policy", "feature_schema"})

    def test_counts_are_summed_from_recorded_telemetry(self) -> None:
        built = summary()
        self.assertEqual(built.decision_counts["loops"], 2)
        self.assertEqual(built.decision_counts["submit"], 1)
        self.assertEqual(built.decision_counts["cancel"], 3)
        self.assertEqual(built.decision_counts["risk_reject"], 1)
        self.assertEqual(built.duration_ms, 3_000)

    def test_order_counts_accept_enum_and_plain_status(self) -> None:
        self.assertEqual(summary().order_counts, {"CANCELED": 1, "OPEN": 1})

    def test_accounting_facts_are_passed_through_not_recomputed(self) -> None:
        """SC-10：报告不重新计算 Accounting。"""
        built = summary()
        self.assertEqual(built.fills.value, 1)
        self.assertEqual(built.fees.value, 0.02)
        self.assertEqual(built.realized_pnl.value, -0.5)
        self.assertEqual(built.final_position.value, 0.0)

    def test_missing_facts_stay_unknown(self) -> None:
        built = summary(fills=None, fees=None, realized_pnl=None, unrealized_pnl=None,
                        max_exposure=None, final_position=None, ended_at=None, data_range=None)
        for fact in (built.fills, built.fees, built.realized_pnl, built.unrealized_pnl,
                     built.max_exposure, built.final_position, built.run.ended_at, built.run.data_range):
            with self.subTest(reason=fact.reason):
                self.assertFalse(fact.known)
                self.assertIsNone(fact.value)
                self.assertTrue(fact.reason)

    def test_authority_failures_become_anomalies(self) -> None:
        built = summary(telemetry=(telemetry(authority_valid=False, authority_reason="AUTHORITY_EXPIRED"),))
        self.assertIn("AUTHORITY_EXPIRED", built.anomalies)

    def test_json_and_markdown_are_both_available(self) -> None:
        """SC-8。"""
        import json

        payload = json.loads(summary_to_json(summary()))
        self.assertEqual(payload["run"]["run_id"], "run-1")
        self.assertEqual(payload["run"]["runtime"]["mode"], "TESTNET")
        text = summary_to_markdown(summary())
        self.assertIn("# Run Summary — run-1", text)
        self.assertIn("| readiness_blockers | PRIVATE_LATENCY_UNOBSERVED |", text)
        self.assertIn("UNKNOWN", summary_to_markdown(summary(ended_at=None)))

    def test_markdown_shows_unknown_with_reason(self) -> None:
        text = summary_to_markdown(summary(final_position=None))
        self.assertIn("UNKNOWN (final position not provided)", text)
