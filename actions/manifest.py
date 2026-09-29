"""Action Manifest（P0001.12.3 §3）：AI 能做什么的**唯一正式来源**。

CAPITAL 全部 `unavailable_by_design`（结构性保证：`ActionSpec` 构造时即拒绝把 CAPITAL 标为 AVAILABLE）。
"""

from __future__ import annotations

from actions.types import ActionAvailability, ActionLevel, ActionSpec
from product.types import RuntimeMode

_UNAVAILABLE_CAPITAL = (
    "capital actions are unavailable_by_design in P0001.12.x: the only path to an order is "
    "MakerPolicy -> RiskGate -> ReadinessAuthority -> ExecutionEngine"
)

ACTION_CATALOG: tuple[ActionSpec, ...] = (
    # ---------------------------------------------------------------- L0 READ
    ActionSpec(action_id="inspect.snapshot", name="Inspect current snapshot", level=ActionLevel.READ,
               side_effect="none", result_schema="{snapshot}", availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="explain.entity", name="Explain decision/order/fill/blocker", level=ActionLevel.READ,
               parameters=("kind", "identity"), result_schema="{explanation,facts}",
               availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="compare.runs", name="Compare two runs", level=ActionLevel.READ,
               parameters=("left", "right"), result_schema="{comparison}", availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="query.blockers", name="List unified blockers", level=ActionLevel.READ,
               result_schema="{blockers}", availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="query.health", name="Query system health", level=ActionLevel.READ,
               result_schema="{health}", availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="query.raw_facts", name="Query raw facts", level=ActionLevel.READ,
               parameters=("kind", "identity"), result_schema="{fact}", availability=ActionAvailability.AVAILABLE),
    # ------------------------------------------------------------- L1 PRODUCT
    ActionSpec(action_id="navigate.surface", name="Navigate to a surface", level=ActionLevel.PRODUCT,
               parameters=("surface", "identity"), side_effect="ui_navigation",
               result_schema="{target}", availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="select.entity", name="Select a run/order/decision", level=ActionLevel.PRODUCT,
               parameters=("kind", "identity"), side_effect="ui_selection",
               result_schema="{selection}", availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="replay.control", name="Control replay (play/pause/step/speed/seek)",
               level=ActionLevel.PRODUCT, parameters=("verb", "count", "speed", "ordinal", "ts"),
               side_effect="replay_state", allowed_modes=(RuntimeMode.REPLAY,),
               result_schema="{command,control}", availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="report.generate", name="Generate a run report", level=ActionLevel.PRODUCT,
               parameters=("run_id", "format"), side_effect="none", result_schema="{report}",
               availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="view.configure", name="Change visualization window", level=ActionLevel.PRODUCT,
               parameters=("window_ms", "bucket_ms", "max_points"), side_effect="ui_display",
               result_schema="{bounds}", availability=ActionAvailability.AVAILABLE),
    # ------------------------------------------------------------- L2 RUNTIME
    ActionSpec(action_id="runtime.stop_replay", name="Stop the replay runtime", level=ActionLevel.RUNTIME,
               side_effect="runtime_lifecycle", confirmation_required=True,
               allowed_modes=(RuntimeMode.REPLAY,), result_schema="{stopped}",
               availability=ActionAvailability.AVAILABLE),
    ActionSpec(action_id="runtime.stop_paper", name="Stop the PAPER runtime", level=ActionLevel.RUNTIME,
               side_effect="runtime_lifecycle", confirmation_required=True,
               allowed_modes=(RuntimeMode.PAPER,), result_schema="{stopped}",
               availability=ActionAvailability.UNAVAILABLE_NO_ENTRY_POINT,
               unavailable_reason="no product entry point for PAPER runtime lifecycle yet"),
    ActionSpec(action_id="runtime.stop_testnet", name="Stop the TESTNET runtime", level=ActionLevel.RUNTIME,
               side_effect="runtime_lifecycle", confirmation_required=True,
               allowed_modes=(RuntimeMode.TESTNET,), result_schema="{stopped}",
               availability=ActionAvailability.UNAVAILABLE_NO_ENTRY_POINT,
               unavailable_reason="no product entry point for TESTNET runtime lifecycle yet"),
    ActionSpec(action_id="runtime.request_reconciliation", name="Request reconciliation",
               level=ActionLevel.RUNTIME, side_effect="reconciliation_request",
               confirmation_required=False, result_schema="{requested}",
               availability=ActionAvailability.UNAVAILABLE_NO_ENTRY_POINT,
               unavailable_reason="reconciliation has no controlled product entry point yet (P0001.13)"),
    # ------------------------------------------------------------- L3 CAPITAL
    ActionSpec(action_id="capital.place_order", name="Place an order", level=ActionLevel.CAPITAL,
               parameters=("symbol", "side", "price", "quantity"), side_effect="trading",
               availability=ActionAvailability.UNAVAILABLE_BY_DESIGN, unavailable_reason=_UNAVAILABLE_CAPITAL),
    ActionSpec(action_id="capital.cancel_order", name="Cancel an order manually", level=ActionLevel.CAPITAL,
               parameters=("client_order_id",), side_effect="trading",
               availability=ActionAvailability.UNAVAILABLE_BY_DESIGN, unavailable_reason=_UNAVAILABLE_CAPITAL),
    ActionSpec(action_id="capital.buy_sell", name="Manual buy/sell", level=ActionLevel.CAPITAL,
               parameters=("side", "quantity"), side_effect="trading",
               availability=ActionAvailability.UNAVAILABLE_BY_DESIGN, unavailable_reason=_UNAVAILABLE_CAPITAL),
    ActionSpec(action_id="capital.set_leverage", name="Change leverage", level=ActionLevel.CAPITAL,
               parameters=("leverage",), side_effect="account_configuration",
               availability=ActionAvailability.UNAVAILABLE_BY_DESIGN, unavailable_reason=_UNAVAILABLE_CAPITAL),
    ActionSpec(action_id="capital.set_position", name="Change position", level=ActionLevel.CAPITAL,
               parameters=("target_qty",), side_effect="trading",
               availability=ActionAvailability.UNAVAILABLE_BY_DESIGN, unavailable_reason=_UNAVAILABLE_CAPITAL),
    ActionSpec(action_id="capital.set_risk_limits", name="Change risk limits", level=ActionLevel.CAPITAL,
               parameters=("limits",), side_effect="risk_configuration",
               availability=ActionAvailability.UNAVAILABLE_BY_DESIGN, unavailable_reason=_UNAVAILABLE_CAPITAL),
    ActionSpec(action_id="capital.force_entry_exit", name="Force entry/exit", level=ActionLevel.CAPITAL,
               parameters=("side",), side_effect="trading",
               availability=ActionAvailability.UNAVAILABLE_BY_DESIGN, unavailable_reason=_UNAVAILABLE_CAPITAL),
    ActionSpec(action_id="capital.modify_readiness_authority", name="Modify readiness authority",
               level=ActionLevel.CAPITAL, side_effect="authority",
               availability=ActionAvailability.UNAVAILABLE_BY_DESIGN, unavailable_reason=_UNAVAILABLE_CAPITAL),
)

CATALOG_BY_ID: dict[str, ActionSpec] = {spec.action_id: spec for spec in ACTION_CATALOG}


def spec(action_id: str) -> ActionSpec | None:
    return CATALOG_BY_ID.get(action_id)


def catalog_payload() -> tuple[dict[str, object], ...]:
    """Manifest 产品形态（机器可读；UI/CLI/Agent 共用）。"""
    return tuple(
        {
            "action_id": item.action_id,
            "name": item.name,
            "level": item.level.value,
            "parameters": list(item.parameters),
            "side_effect": item.side_effect,
            "confirmation_required": item.confirmation_required,
            "allowed_modes": [mode.value for mode in item.allowed_modes],
            "result_schema": item.result_schema,
            "availability": item.availability.value,
            "unavailable_reason": item.unavailable_reason,
        }
        for item in ACTION_CATALOG
    )


__all__ = ["ACTION_CATALOG", "CATALOG_BY_ID", "catalog_payload", "spec"]
