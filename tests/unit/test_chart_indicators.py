"""P0001.17 §5：UI **不得**自行计算业务指标（MA/EMA/VOL 由成熟图表库计算）。

本测试同时是**架构守卫**：`ui/pages/market/indicators.js` 只能声明指标能力与"后端无该事实"的原因，
不得导出任何本地业务指标计算函数（VWAP/ATR 曾在此自算 —— 人类裁决明确禁止）。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
INDICATORS = PROJECT_ROOT / "ui" / "pages" / "market" / "indicators.js"

NODE_SCRIPT = """
import * as mod from %s;
console.log(JSON.stringify({
  exports: Object.keys(mod).sort(),
  available: mod.PROBEX_INDICATORS.map((i) => i.name),
  backendFacts: mod.BACKEND_FACT_INDICATORS.map((i) => ({ name: i.name, source: i.source })),
  registers: mod.registerProbexIndicators(),
}));
"""

#: 允许出现在 UI 的指标（由图表库计算/绘制）
ALLOWED_LIBRARY_INDICATORS = {"MA", "EMA", "VOL"}
#: 禁止在 UI 计算的指标（后端无事实 ⇒ 只能 NOT_AVAILABLE + reason）
FORBIDDEN_LOCAL_COMPUTATION = ("vwap", "atr")


@unittest.skipIf(shutil.which("node") is None, "node is required for the UI indicator contract test")
class IndicatorContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        script = NODE_SCRIPT % json.dumps(str(INDICATORS))
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[-400:]}")
        cls.values = json.loads(result.stdout.strip().splitlines()[-1])

    def test_no_self_computed_business_indicators_are_exported(self) -> None:
        """§5 守卫：不得导出 vwap/atr 之类的本地业务计算。"""
        for name in FORBIDDEN_LOCAL_COMPUTATION:
            with self.subTest(indicator=name):
                self.assertNotIn(name, self.values["exports"])

    def test_only_library_indicators_are_offered(self) -> None:
        self.assertEqual(set(self.values["available"]), ALLOWED_LIBRARY_INDICATORS)

    def test_backend_fact_indicators_declare_their_source(self) -> None:
        """VWAP/ATR 必须声明**后端事实来源**（而不是在 UI 计算）。"""
        facts = {item["name"]: item["source"] for item in self.values["backendFacts"]}
        self.assertEqual(set(facts), {"VWAP", "ATR"})
        for name, source in facts.items():
            with self.subTest(indicator=name):
                self.assertTrue(source and len(source) > 10, "source must name the backend fact")

    def test_registration_is_a_noop(self) -> None:
        self.assertFalse(self.values["registers"])


if __name__ == "__main__":
    unittest.main()
