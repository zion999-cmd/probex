"""live 测试共用的采集/统计助手（P0001.9.1.1）：原始 depthUpdate 样本与连续性统计。"""

from __future__ import annotations

import statistics
import time

from connectors.binance.market_data.streams import StreamMessage, parse_message


def capture_depth_updates(ws, *, max_events: int, timeout_s: float) -> list[dict]:
    """从已订阅的连接连续采集原始 `depthUpdate` 报文（不去重、不改写）。"""
    payloads: list[dict] = []
    deadline = time.monotonic() + timeout_s
    while len(payloads) < max_events and time.monotonic() < deadline:
        try:
            text = ws.recv_text(timeout_s=min(10.0, max(1.0, deadline - time.monotonic())))
        except Exception:
            break
        if text is None:
            break
        message: StreamMessage | None = parse_message(text)
        if message is None or message.data.get("e") != "depthUpdate":
            continue
        payloads.append(message.data)
    return payloads


def depth_rule_stats(payloads: list[dict]) -> dict:
    """统计三条连续性规则在相邻对上的通过情况（全为真实样本，不做任何修正）。"""
    pairs = list(zip(payloads, payloads[1:]))
    rule_a = sum(1 for prev, cur in pairs if cur["U"] == prev["u"] + 1)
    rule_b = sum(1 for prev, cur in pairs if cur.get("pu") == prev["u"])
    rule_w = sum(1 for prev, cur in pairs if cur["U"] <= prev["u"] + 1 <= cur["u"])
    pu_present = sum(1 for payload in payloads if "pu" in payload)
    gaps_u = [cur["U"] - prev["u"] for prev, cur in pairs]
    old_gap_new_contiguous = [
        {
            "prev_u": prev["u"],
            "current_U": cur["U"],
            "current_u": cur["u"],
            "current_pu": cur.get("pu"),
        }
        for prev, cur in pairs
        if cur.get("pu") == prev["u"] and cur["U"] != prev["u"] + 1
    ]
    total = len(pairs)
    return {
        "pairs": total,
        "pu_present": f"{pu_present}/{len(payloads)}",
        "rule_A_strict_adjacent": f"{rule_a}/{total}",
        "rule_B_pu_eq_prev_u": f"{rule_b}/{total}",
        "rule_W_window": f"{rule_w}/{total}",
        "pu_minus_prev_u_values": sorted({(cur.get("pu", -1) - prev["u"]) for prev, cur in pairs}),
        "U_minus_prev_u_median": int(statistics.median(gaps_u)) if gaps_u else None,
        "old_gap_new_contiguous": old_gap_new_contiguous,
    }


__all__ = ["capture_depth_updates", "depth_rule_stats"]
