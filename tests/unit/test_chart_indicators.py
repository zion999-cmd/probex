"""Indicator math（renderer-independent）—— 用 Node 真实执行 `ui/pages/market/indicators.js`。"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
INDICATORS = PROJECT_ROOT / "ui" / "pages" / "market" / "indicators.js"

NODE_SCRIPT = """
import { sma, ema, vwap, atr } from %s;
const bars = [
  { open: 10, high: 12, low: 9, close: 11, volume: 2 },
  { open: 11, high: 13, low: 10, close: 12, volume: 3 },
  { open: 12, high: 14, low: 11, close: 13, volume: 5 },
  { open: 13, high: 15, low: 12, close: 14, volume: 0 },
];
console.log(JSON.stringify({
  sma: sma([1, 2, 3, 4], 2),
  ema: ema([1, 2, 3], 2),
  vwap: vwap(bars, 3),
  atr: atr(bars, 2),
  vwapNoVolume: vwap([{ open: 1, high: 12, low: 9, close: 11, volume: 0 }], 1),
}));
"""


@unittest.skipIf(shutil.which("node") is None, "node is required for indicator math tests")
class IndicatorMathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        script = NODE_SCRIPT % json.dumps(str(INDICATORS))
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[-400:]}")
        cls.values = json.loads(result.stdout.strip().splitlines()[-1])

    def test_sma(self) -> None:
        self.assertEqual(self.values["sma"], [None, 1.5, 2.5, 3.5])

    def test_ema_seeds_with_first_value(self) -> None:
        ema = self.values["ema"]
        self.assertAlmostEqual(ema[0], 1.0)
        self.assertAlmostEqual(ema[1], 1.6666666666666667, places=6)
        self.assertAlmostEqual(ema[2], 2.5555555555555554, places=6)

    def test_vwap_uses_volume_weights(self) -> None:
        vwap = self.values["vwap"]
        # typical = (high+low+close)/3: bar0 10.6667(v2), bar1 11.6667(v3), bar2 12.6667(v5), bar3 13.6667(v0)
        self.assertAlmostEqual(vwap[2], (10.666666666666666 * 2 + 11.666666666666666 * 3
                                         + 12.666666666666666 * 5) / 10, places=6)
        # 窗口 [1,2,3]：总量 8（bar3 volume=0 不计权）
        self.assertAlmostEqual(vwap[3], (11.666666666666666 * 3 + 12.666666666666666 * 5) / 8, places=6)

    def test_vwap_falls_back_to_typical_when_no_volume(self) -> None:
        # 全 0 成交量的窗口 ⇒ 退化为 typical 均价（不产生 NaN/0）
        self.assertAlmostEqual(self.values["vwapNoVolume"][0], (12 + 9 + 11) / 3, places=6)

    def test_atr_is_positive_and_seeded_after_period(self) -> None:
        atr = self.values["atr"]
        self.assertIsNone(atr[0])
        self.assertIsNotNone(atr[1])
        self.assertGreater(atr[1], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
