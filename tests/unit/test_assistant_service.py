"""P0001.12.3 Assistant 单测（SC-1/2/11）：上下文与建议动作，全部来自既有事实与 Manifest。"""

from __future__ import annotations

import unittest

from actions import ActionContext, ActionGateway, ActionAuditLog, ConfirmationRegistry, HandlerResult
from assistant import AssistantService
from product.types import RuntimeMode
from tests.unit.test_product_snapshot import service


class AssistantServiceTest(unittest.TestCase):
    def gateway(self) -> ActionGateway:
        gw = ActionGateway(clock=lambda: 1_000, confirmations=ConfirmationRegistry(ttl_ms=30_000),
                           audit=ActionAuditLog())
        gw.register("navigate.surface", lambda request, ctx: HandlerResult(result={"target": "#/market"}))
        gw.register("replay.control", lambda request, ctx: HandlerResult(result={"verb": "play"}))
        gw.register("runtime.stop_replay", lambda request, ctx: HandlerResult(result={"stopped": True}))
        return gw

    def assistant(self, mode: RuntimeMode = RuntimeMode.REPLAY, **overrides: object) -> AssistantService:
        identity_fn = __import__("tests.unit.test_product_snapshot", fromlist=["identity"]).identity
        snapshot = service(identity=identity_fn(mode), **overrides).snapshot()
        return AssistantService(snapshot_provider=lambda: snapshot, gateway=self.gateway())

    def test_1_context_references_runtime_and_selection(self) -> None:
        ctx = self.assistant().context(surface="activity", selected={"order": "probex-s1-1", "run": "rt-1"},
                                       replay_position=42)

        self.assertEqual(ctx.surface, "activity")
        self.assertEqual(ctx.runtime.mode, RuntimeMode.REPLAY)
        self.assertEqual(ctx.selected_order_id.value, "probex-s1-1")
        self.assertEqual(ctx.selected_run_id.value, "rt-1")
        self.assertEqual(ctx.replay_position.value, 42)
        self.assertTrue(ctx.active_blockers)

    def test_1_missing_selection_stays_unknown(self) -> None:
        ctx = self.assistant().context()
        self.assertFalse(ctx.selected_fill_id.known)
        self.assertFalse(ctx.replay_position.known)

    def test_11_suggestions_come_only_from_the_manifest(self) -> None:
        assistant = self.assistant(RuntimeMode.REPLAY)
        ctx = assistant.context()
        suggested = {entry["action_id"] for entry in assistant.suggested_actions(ctx)}
        available = {entry["action_id"] for entry in assistant.gateway.manifest()
                     if entry["available"] and "REPLAY" in entry["allowed_modes"]}

        self.assertEqual(suggested, available)
        self.assertNotIn("capital.place_order", suggested)

    def test_suggestions_respect_mode_restrictions(self) -> None:
        replay_assistant = self.assistant(RuntimeMode.REPLAY)
        testnet_assistant = self.assistant(RuntimeMode.TESTNET)

        replay_actions = {e["action_id"] for e in replay_assistant.suggested_actions(
            replay_assistant.context())}
        testnet_actions = {e["action_id"] for e in testnet_assistant.suggested_actions(
            testnet_assistant.context())}

        self.assertIn("replay.control", replay_actions)
        self.assertNotIn("replay.control", testnet_actions)
        self.assertIn("navigate.surface", testnet_actions)

    def test_2_explain_composes_existing_facts_without_an_llm(self) -> None:
        assistant = self.assistant()
        explanation = assistant.explain("decision", "pred-1")

        self.assertEqual(explanation["kind"], "decision")
        self.assertTrue(explanation["trace"])
        self.assertIn("no LLM", explanation["note"])
        self.assertTrue(explanation["blockers"])

    def test_action_context_carries_the_run_identity(self) -> None:
        assistant = self.assistant(RuntimeMode.TESTNET)
        action_context = assistant.action_context(assistant.context(surface="system"))

        self.assertIsInstance(action_context, ActionContext)
        self.assertEqual(action_context.surface, "system")
        self.assertEqual(action_context.mode, RuntimeMode.TESTNET)
