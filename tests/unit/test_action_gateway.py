"""P0001.12.3 Action Gateway 单测（SC-2/5/6/7/8/9/10/11/12/13/14）。"""

from __future__ import annotations

import pathlib
import unittest

from actions import (
    ACTION_CATALOG,
    ActionAuditLog,
    ActionAvailability,
    ActionContext,
    ActionGateway,
    ActionGatewayError,
    ActionLevel,
    ActionOutcomeUnknown,
    ActionRequest,
    ActionResult,
    ActionStatus,
    ConfirmationRegistry,
    HandlerResult,
    catalog_payload,
    spec,
)
from product.types import RuntimeMode

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = [1_000]


def clock() -> int:
    return NOW[0]


def gateway(**overrides: object) -> ActionGateway:
    values: dict[str, object] = {"clock": clock, "confirmations": ConfirmationRegistry(ttl_ms=30_000),
                                 "audit": ActionAuditLog(capacity=100)}
    values.update(overrides)
    return ActionGateway(**values)  # type: ignore[arg-type]


def context(mode: RuntimeMode = RuntimeMode.REPLAY, runtime_id: str = "rt-1") -> ActionContext:
    return ActionContext(runtime_id=runtime_id, mode=mode, environment="testnet", venue="binance",
                         symbol="BTCUSDT", surface="market")


class CatalogTest(unittest.TestCase):
    def test_10_capital_actions_are_all_unavailable_by_design(self) -> None:
        capital = [item for item in ACTION_CATALOG if item.level is ActionLevel.CAPITAL]

        self.assertTrue(capital)
        for item in capital:
            with self.subTest(action=item.action_id):
                self.assertIs(item.availability, ActionAvailability.UNAVAILABLE_BY_DESIGN)
                self.assertTrue(item.unavailable_reason)

    def test_capital_spec_cannot_be_declared_available(self) -> None:
        from actions.types import ActionSpec

        with self.assertRaises(ValueError):
            ActionSpec(action_id="capital.hack", name="hack", level=ActionLevel.CAPITAL,
                       availability=ActionAvailability.AVAILABLE)

    def test_levels_are_fixed_to_four(self) -> None:
        self.assertEqual({item.level for item in ACTION_CATALOG},
                         {ActionLevel.READ, ActionLevel.PRODUCT, ActionLevel.RUNTIME, ActionLevel.CAPITAL})

    def test_manifest_payload_describes_every_action(self) -> None:
        for entry in catalog_payload():
            with self.subTest(action=entry["action_id"]):
                for field in ("action_id", "name", "level", "parameters", "side_effect",
                              "confirmation_required", "allowed_modes", "result_schema", "availability"):
                    self.assertIn(field, entry)


class GatewayTest(unittest.TestCase):
    def test_capital_invocation_is_refused(self) -> None:
        result = gateway().invoke(ActionRequest(action_id="capital.place_order", requested_by="ui"),
                                  context())

        self.assertIs(result.status, ActionStatus.REFUSED)
        self.assertEqual(result.reason_code, "UNAVAILABLE_BY_DESIGN")

    def test_capital_handler_cannot_be_registered(self) -> None:
        with self.assertRaises(ActionGatewayError):
            gateway().register("capital.set_leverage", lambda request, ctx: HandlerResult())

    def test_unknown_action_is_refused(self) -> None:
        result = gateway().invoke(ActionRequest(action_id="nope.nope"), context())

        self.assertIs(result.status, ActionStatus.REFUSED)
        self.assertEqual(result.reason_code, "UNKNOWN_ACTION")

    def test_product_action_runs_through_the_gateway(self) -> None:
        gw = gateway()
        gw.register("navigate.surface", lambda request, ctx: HandlerResult(
            result={"target": "#/market"}, fact_refs=("surface:market",)))

        result = gw.invoke(ActionRequest(action_id="navigate.surface", parameters={"surface": "market"},
                                         requested_by="ui"), context())

        self.assertIs(result.status, ActionStatus.SUCCEEDED)
        self.assertEqual(result.result["target"], "#/market")
        self.assertEqual(result.resulting_fact_refs, ("surface:market",))

    def test_7_runtime_action_requires_confirmation(self) -> None:
        gw = gateway()
        called: list[str] = []
        gw.register("runtime.stop_replay", lambda request, ctx: HandlerResult(result={"stopped": True})
                    if called.append("stop") is None else HandlerResult())

        denied = gw.invoke(ActionRequest(action_id="runtime.stop_replay"), context())
        self.assertIs(denied.status, ActionStatus.CONFIRMATION_REQUIRED)
        self.assertIsNotNone(denied.confirmation_id)
        self.assertEqual(called, [])

        confirmed = gw.invoke(ActionRequest(action_id="runtime.stop_replay",
                                            confirmation=denied.confirmation_id), context())
        self.assertIs(confirmed.status, ActionStatus.SUCCEEDED)
        self.assertEqual(called, ["stop"])

    def test_8_confirmation_cannot_be_replayed_or_cross_scopes(self) -> None:
        gw = gateway()
        gw.register("runtime.stop_replay", lambda request, ctx: HandlerResult(result={"stopped": True}))
        first = gw.invoke(ActionRequest(action_id="runtime.stop_replay"), context())
        gw.invoke(ActionRequest(action_id="runtime.stop_replay", confirmation=first.confirmation_id),
                  context())

        replayed = gw.invoke(ActionRequest(action_id="runtime.stop_replay",
                                           confirmation=first.confirmation_id), context())
        self.assertIs(replayed.status, ActionStatus.REFUSED)
        self.assertIn("CONFIRMATION_INVALID", replayed.reason_code)

        wrong_runtime = gw.invoke(ActionRequest(action_id="runtime.stop_replay"), context())
        crossed = gw.invoke(ActionRequest(action_id="runtime.stop_replay",
                                          confirmation=wrong_runtime.confirmation_id),
                            context(runtime_id="rt-2"))
        self.assertIs(crossed.status, ActionStatus.REFUSED)
        self.assertIn("does not match this runtime", crossed.reason_code)

    def test_confirmation_expires(self) -> None:
        gw = gateway()
        gw.register("runtime.stop_replay", lambda request, ctx: HandlerResult())
        pending = gw.invoke(ActionRequest(action_id="runtime.stop_replay"), context())
        NOW[0] += 60_000
        try:
            expired = gw.invoke(ActionRequest(action_id="runtime.stop_replay",
                                              confirmation=pending.confirmation_id), context())
            self.assertIs(expired.status, ActionStatus.REFUSED)
            self.assertIn("expired", expired.reason_code)
        finally:
            NOW[0] = 1_000

    def test_confirmation_is_bound_to_parameters_and_action(self) -> None:
        """SC-8：确认绑定参数与 action —— 参数不同 ⇒ 拒绝；action 不同 ⇒ 拒绝。"""
        from actions import ConfirmationError

        registry = ConfirmationRegistry(ttl_ms=30_000)
        issued = registry.issue(action_id="runtime.stop_replay", parameters={"reason": "maintenance"},
                                runtime_id="rt-1", now_ms=1_000)
        with self.assertRaises(ConfirmationError):
            registry.consume(issued.confirmation_id, action_id="runtime.stop_replay",
                             parameters={"reason": "other"}, runtime_id="rt-1", now_ms=1_100)
        issued2 = registry.issue(action_id="runtime.stop_replay", parameters={}, runtime_id="rt-1",
                                 now_ms=1_000)
        with self.assertRaises(ConfirmationError):
            registry.consume(issued2.confirmation_id, action_id="runtime.stop_paper", parameters={},
                             runtime_id="rt-1", now_ms=1_100)

    def test_mode_restriction_is_enforced(self) -> None:
        gw = gateway()
        gw.register("replay.control", lambda request, ctx: HandlerResult())
        result = gw.invoke(ActionRequest(action_id="replay.control", parameters={"verb": "play"}),
                           context(mode=RuntimeMode.TESTNET))

        self.assertIs(result.status, ActionStatus.REFUSED)
        self.assertIn("MODE_NOT_ALLOWED", result.reason_code)

    def test_13_unknown_outcome_is_not_failed_or_succeeded(self) -> None:
        gw = gateway()

        def handler(request: ActionRequest, ctx: ActionContext) -> HandlerResult:
            raise ActionOutcomeUnknown("runtime state unknown after stop request")

        gw.register("runtime.stop_replay", handler)
        pending = gw.invoke(ActionRequest(action_id="runtime.stop_replay"), context())
        result = gw.invoke(ActionRequest(action_id="runtime.stop_replay",
                                         confirmation=pending.confirmation_id), context())

        self.assertIs(result.status, ActionStatus.UNKNOWN)
        self.assertNotEqual(result.status, ActionStatus.FAILED)
        self.assertNotEqual(result.status, ActionStatus.SUCCEEDED)

    def test_handler_failure_is_failed(self) -> None:
        gw = gateway()
        gw.register("navigate.surface", lambda request, ctx: (_ for _ in ()).throw(ValueError("boom")))
        result = gw.invoke(ActionRequest(action_id="navigate.surface"), context())

        self.assertIs(result.status, ActionStatus.FAILED)
        self.assertEqual(result.reason_code, "ValueError")

    def test_9_every_invocation_is_audited_including_refusals(self) -> None:
        gw = gateway()
        gw.register("navigate.surface", lambda request, ctx: HandlerResult(result={"target": "#/system"}))
        gw.invoke(ActionRequest(action_id="navigate.surface", requested_by="agent"), context())
        gw.invoke(ActionRequest(action_id="capital.place_order", requested_by="agent"), context())
        gw.invoke(ActionRequest(action_id="unknown.action", requested_by="agent"), context())

        entries = gw.audit.entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual([entry.status for entry in entries],
                         [ActionStatus.SUCCEEDED, ActionStatus.REFUSED, ActionStatus.REFUSED])
        self.assertEqual({entry.actor for entry in entries}, {"agent"})
        # 参数只记录指纹（不原样留存输入）
        self.assertTrue(all(entry.parameters_fingerprint.startswith("sha256:") for entry in entries))

    def test_12_gateway_has_no_trading_capability(self) -> None:
        for name in ("types.py", "gateway.py", "manifest.py", "confirmation.py", "audit.py"):
            source = (PROJECT_ROOT / "actions" / name).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("from ") or stripped.startswith("import "):
                    for root in ("execution", "connectors", "strategy", "risk", "live"):
                        with self.subTest(module=name, import_line=stripped):
                            self.assertFalse(stripped.startswith(f"from {root}.")
                                             or stripped.startswith(f"import {root}"))
            # 说明性文字里可以出现 RiskGate/MakerPolicy 的名字（解释边界），但**不得**出现任何调用面
            for forbidden in ("BinanceExecutionAdapter", "submit_post_only_limit", "risk.gate",
                              "execution.engine", "MakerPolicy("):
                self.assertNotIn(forbidden, source)

    def test_registered_handlers_are_listed_in_the_manifest(self) -> None:
        gw = gateway()
        gw.register("report.generate", lambda request, ctx: HandlerResult())

        manifest = {entry["action_id"]: entry for entry in gw.manifest()}
        self.assertTrue(manifest["report.generate"]["available"])
        self.assertFalse(manifest["runtime.request_reconciliation"]["available"])
        self.assertEqual(manifest["capital.place_order"]["availability"], "UNAVAILABLE_BY_DESIGN")

    def test_spec_lookup_and_unavailable_actions_carry_reasons(self) -> None:
        self.assertIsNotNone(spec("replay.control"))
        for item in ACTION_CATALOG:
            if item.availability is not ActionAvailability.AVAILABLE:
                with self.subTest(action=item.action_id):
                    self.assertTrue(item.unavailable_reason)
