"""P0017.5 §3.4/§4-B3：实时多标的模拟交易产品入口的校验（不触网）。

真实公开行情连接需要可访问 Binance 的外部环境（当前 451/超时），因此这里只验证
CLI/服务在**建立网络连接之前**对缺配置、<2 标的、重复标的等给出明确失败；
真实长跑是独立 NOT RUN 的外部 smoke。
"""

from __future__ import annotations

import io
import json
import pathlib
import unittest

from cli.main import main
from runtime.live_paper_service import build_live_paper_runner

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
PROFILE = PROJECT_ROOT / "profiles" / "live-paper-multi-symbol.json"


class LivePaperEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.values = json.loads(PROFILE.read_text(encoding="utf-8"))

    def test_less_than_two_symbols_fails_before_network(self) -> None:
        bad = dict(self.values)
        bad["live.symbols"] = ["BTCUSDT"]
        with self.assertRaises(ValueError):
            build_live_paper_runner(bad)

    def test_duplicate_symbols_fails_before_network(self) -> None:
        bad = dict(self.values)
        bad["live.symbols"] = ["BTCUSDT", "BTCUSDT"]
        with self.assertRaises(ValueError):
            build_live_paper_runner(bad)

    def test_missing_required_config_fails_before_network(self) -> None:
        with self.assertRaises(ValueError):
            build_live_paper_runner({"live.symbols": ["BTCUSDT", "ETHUSDT"]})

    def test_cli_missing_config_returns_usage_exit_code(self) -> None:
        out = io.StringIO()
        rc = main(["live-paper", "--config", "/nonexistent/live.json"], stdout=out, stderr=io.StringIO())
        self.assertEqual(rc, 2)
        payload = json.loads(out.getvalue().strip())
        self.assertEqual(payload["status"], "INVALID_INPUT")

    def test_profile_documents_paper_only_boundary(self) -> None:
        text = PROFILE.read_text(encoding="utf-8").lower()
        self.assertIn("simulated", text)
        self.assertIn("no exchange write", text)


if __name__ == "__main__":
    unittest.main()
