"""Capability Manifest（P0001.11 §5 / 裁决 E）。

**尽量从注册表生成**（API 路由表 + CLI 命令表），避免手写漂移。
`api.write` 恒为 `unavailable_by_design` —— 这是产品边界，不是临时状态：
任何未来的写能力都必须显式改动本文件与对应测试，不能"偷偷长出来"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

WRITE_UNAVAILABLE = "unavailable_by_design"

#: 未实现但已预留的端点（返回 501，不提供任何能力）
RESERVED_ENDPOINTS: tuple[str, ...] = ("/api/v1/runtime/stop",)

#: P0001.12：simulation control（**不是**交易写路径；只作用于 REPLAY runtime）
SIMULATION_CONTROL: tuple[str, ...] = (
    "/api/v1/replay/play", "/api/v1/replay/pause", "/api/v1/replay/step",
    "/api/v1/replay/speed", "/api/v1/replay/seek",
)

#: 明确"不提供"的能力类别（供 Agent/UI 直接回答"我能不能下单"）
UNAVAILABLE_ACTIONS: tuple[str, ...] = (
    "place_order", "cancel_order", "modify_order", "flatten_position",
    "set_leverage", "set_risk_limits", "edit_config", "start_stop_runtime",
)


def build_capabilities_manifest(
    *,
    schema_version: str,
    api_read: Sequence[str],
    cli_commands: Mapping[str, str],
    exit_codes: Mapping[str, str],
    extra_read: Sequence[str] = (),
) -> dict[str, object]:
    """生成机器可读能力清单（UI / CLI / AI 共用）。"""
    read_paths = sorted({*api_read, *extra_read})
    return {
        "schema_version": schema_version,
        "api": {
            "read": read_paths,
            #: 交易写能力仍然不可用（产品边界）；replay 控制单独列出，且只作用于 REPLAY runtime
            "write": WRITE_UNAVAILABLE,
            "simulation_control": sorted(SIMULATION_CONTROL),
            #: P0001.12.3：受控 Action Plane（L0/L1 自动、L2 需确认、L3 恒不可用）
            "action_plane": {
                "manifest": "/api/v1/actions",
                "audit": "/api/v1/actions/audit",
                "invoke": "/api/v1/actions/<action_id>",
                "capital": "unavailable_by_design",
            },
            "reserved": sorted(RESERVED_ENDPOINTS),
        },
        "cli": {"commands": dict(sorted(cli_commands.items())), "exit_codes": dict(sorted(exit_codes.items()))},
        "unavailable_actions": list(UNAVAILABLE_ACTIONS),
        "notes": (
            "Probex exposes read-only product surfaces. The only path to an order is "
            "MakerPolicy -> RiskGate -> ReadinessAuthority -> ExecutionEngine."
        ),
    }


__all__ = [
    "RESERVED_ENDPOINTS",
    "SIMULATION_CONTROL",
    "UNAVAILABLE_ACTIONS",
    "WRITE_UNAVAILABLE",
    "build_capabilities_manifest",
]
