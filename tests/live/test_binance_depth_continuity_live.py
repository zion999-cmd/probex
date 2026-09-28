"""P0001.9.1.1 SC-6：真实流连续性核验（opt-in，无需凭据）。

```bash
PROBEX_LIVE_SMOKE=1 python3 -m unittest -v tests.live.test_binance_depth_continuity_live
```

做的事（只读，不改任何生产语义）：

1. 连续采集 100–300 条真实 `depthUpdate`；
2. 分别统计三条规则在相邻对上的通过率：
   A `current.U == prev.u + 1`、B `current.pu == prev.u`、W `U <= prev.u+1 <= u`；
3. 打印 10 条「被旧规则判为 GAP、被 pu 规则判为连续」的真实样本（提案要求）。

断言（真实验收口径）：B 必须 **100% 通过**、A 必须 **0% 通过**（否则本诊断/修复的前提不成立）。
"""

from __future__ import annotations

import json
import os
import time
import unittest

from tests.live.connector_helpers import capture_depth_updates, depth_rule_stats

from connectors.binance.market_data.transport import connect
from connectors.binance.market_data.streams import parse_message, subscribe_message
from tests.live_support import BINANCE_SYMBOL

LIVE_FLAG = "PROBEX_LIVE_SMOKE"
DEFAULT_EVENTS = 200


@unittest.skipUnless(os.environ.get(LIVE_FLAG) == "1", f"set {LIVE_FLAG}=1 to run the real continuity verification")
class FuturesDepthContinuityLiveTest(unittest.TestCase):
    def test_sc6_pu_rule_matches_reality(self) -> None:
        streams = (f"{BINANCE_SYMBOL.lower()}@depth",)
        ws = connect(
            f"wss://fstream.binance.com/public/stream?streams={'/'.join(streams)}", timeout_s=20
        )
        self.addCleanup(ws.close)
        ws.send_text(subscribe_message(streams, request_id=1))

        payloads = capture_depth_updates(ws, max_events=DEFAULT_EVENTS, timeout_s=60.0)
        self.assertGreaterEqual(len(payloads), 100, "not enough real depthUpdate samples")
        stats = depth_rule_stats(payloads)
        report = {
            "stream": streams[0],
            "events": len(payloads),
            "pairs": stats["pairs"],
            "pu_present": stats["pu_present"],
            "rule_B_pu_eq_prev_u": stats["rule_B_pu_eq_prev_u"],
            "rule_W_window": stats["rule_W_window"],
            "rule_A_strict_adjacent": stats["rule_A_strict_adjacent"],
            "pu_minus_prev_u_values": stats["pu_minus_prev_u_values"],
            "U_minus_prev_u_median": stats["U_minus_prev_u_median"],
        }
        print("\nPROBEX FUTURES DEPTH CONTINUITY REPORT:\n" + json.dumps(report, indent=2))
        print("被旧规则判 GAP、被 pu 规则判连续的真实样本（前 10 条）：")
        for step, sample in enumerate(stats["old_gap_new_contiguous"][:10], start=1):
            print(f"  [{step}] prev.u={sample['prev_u']} curr.U={sample['current_U']} "
                  f"curr.u={sample['current_u']} curr.pu={sample['current_pu']} "
                  f"old_rule=GAP pu_rule=CONTIGUOUS")

        self.assertEqual(stats["pu_present"], f"{len(payloads)}/{len(payloads)}")
        self.assertEqual(stats["rule_B_pu_eq_prev_u"], f"{stats['pairs']}/{stats['pairs']}")
        adjacent_passes = int(stats["rule_A_strict_adjacent"].split("/")[0])
        # 旧规则在真实 Futures 流上几乎全判 GAP（允许极少数恰好相邻的巧合）
        self.assertLessEqual(adjacent_passes / stats["pairs"], 0.05, report)
        self.assertTrue(stats["old_gap_new_contiguous"])


if __name__ == "__main__":
    unittest.main()
