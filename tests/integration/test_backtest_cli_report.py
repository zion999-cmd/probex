"""P0017.3 Batch 3/4：CLI、manifest/报告、双跑等价与状态机（离线）。"""

from __future__ import annotations

import io
import json
import pathlib
import shutil
import unittest

from cli.main import main as cli_main
from backtest import BacktestStatus
from backtest.driver import BacktestConfig, run_backtest
from backtest.identity import dataset_identity, run_identity
from backtest.report import write_artifacts
from backtest.prediction import local_trial_runtime
from runtime.maker_config import build_maker_policy
from tests.ui.market_fixture import write_market_store


PROFILE = (pathlib.Path(__file__).resolve().parents[2] / "profiles" / "backtest-default.json")


def _policy():
    return build_maker_policy(json.loads(PROFILE.read_text(encoding="utf-8")))


def _cfg(**over) -> BacktestConfig:
    v = json.loads(PROFILE.read_text(encoding="utf-8"))
    base = dict(
        symbol=v["backtest.symbol"], initial_balance=v["backtest.initial_balance"],
        maker_fee_rate=v["backtest.maker_fee_rate"], fee_asset=v["backtest.fee_asset"],
        submit_latency_ms=v["backtest.submit_latency_ms"],
        cancel_latency_ms=v["backtest.cancel_latency_ms"],
        max_position_notional=v.get("risk.max_position_notional"),
        max_open_order_exposure=v.get("risk.max_open_order_exposure"),
        seed=v.get("backtest.seed"))
    base.update(over)
    return BacktestConfig(**base)


class BacktestArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-art-"))
        self.store = self.tmp / "events.jsonl"
        write_market_store(self.store, hours=1, mark_price=60_000.0)
        self.policy = _policy()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, out: str):
        ident = dataset_identity([self.store])
        run_id, _ = run_identity(code_version="test", dataset_sha256=ident["sha256"],
                                   config=_cfg().canonical(), seed=7)
        return run_backtest(self.store, _cfg(), run_id=run_id,
                             dataset_sha256=ident["sha256"],
                             decision_fn=lambda **k: self.policy.decide(**k),
                             prediction_runtime=local_trial_runtime())

    def test_artifacts_written_and_self_consistent(self) -> None:
        result = self._run("o1")
        paths = write_artifacts(result, out_dir=self.tmp / "o1", dataset_files=["events.jsonl"])
        manifest = json.loads(paths["manifest"].read_text())
        summary = json.loads(paths["summary"].read_text())
        self.assertEqual(manifest["run_id"], summary["run_id"])
        self.assertEqual(manifest["final_status"], "SUCCESS")
        self.assertIn("cost_models", manifest)
        # 未建模成本显式 UNSUPPORTED，不写成零
        self.assertEqual(manifest["cost_models"]["slippage"], "UNSUPPORTED")
        self.assertEqual(summary["capability_declarations"]["slippage_model"], "UNSUPPORTED")
        self.assertGreater(summary["trace"]["fills"], 0)

    def test_identical_runs_identical_result_hash(self) -> None:
        r1 = self._run("a")
        r2 = self._run("b")
        p1 = write_artifacts(r1, out_dir=self.tmp / "a", dataset_files=["events.jsonl"])
        p2 = write_artifacts(r2, out_dir=self.tmp / "b", dataset_files=["events.jsonl"])
        self.assertEqual(p1["result_hash"], p2["result_hash"])
        # 终态权益一致
        self.assertEqual(r1.accounting.equity(), r2.accounting.equity())

    def test_input_change_changes_run_identity(self) -> None:
        c = _cfg()
        d1 = dataset_identity([self.store])["sha256"]
        id1, _ = run_identity(code_version="test", dataset_sha256=d1, config=c.canonical(), seed=7)
        id2, _ = run_identity(code_version="test", dataset_sha256="0" * 64, config=c.canonical(), seed=7)
        self.assertNotEqual(id1, id2)
        id3, _ = run_identity(code_version="test", dataset_sha256=d1, config=c.canonical(), seed=8)
        self.assertNotEqual(id1, id3)


class BacktestCliTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-cli-"))
        self.store = self.tmp / "events.jsonl"
        write_market_store(self.store, hours=1, mark_price=60_000.0)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_success_exit0_and_artifacts(self) -> None:
        out = self.tmp / "out"
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli_main(["backtest", "--dataset", str(self.store),
                          "--config", str(PROFILE), "--out", str(out)],
                         stdout=stdout, stderr=stderr)
        self.assertEqual(code, 0, stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertTrue((out / "manifest.json").exists())
        self.assertTrue((out / "summary.json").exists())
        self.assertTrue((out / "report.md").exists())

    def test_cli_missing_dataset_is_invalid_input(self) -> None:
        stdout = io.StringIO()
        code = cli_main(["backtest", "--dataset", str(self.tmp / "nope.jsonl"),
                          "--config", str(PROFILE), "--out", str(self.tmp / "x")],
                         stdout=stdout, stderr=io.StringIO())
        self.assertEqual(code, 2)  # INVALID_INPUT → EXIT_USAGE
        self.assertEqual(json.loads(stdout.getvalue())["status"], "INVALID_INPUT")

    def test_cli_no_trade_write_in_command_tree(self) -> None:
        # backtest 子命令只接受本地 dataset/config/out，无任何 API url / key 参数
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli_main(["backtest", "--help"], stdout=stdout, stderr=stderr)
        self.assertEqual(code, 0)
        help_text = stdout.getvalue() + stderr.getvalue()
        for forbidden in ("api-key", "secret", "live", "testnet"):
            self.assertNotIn(forbidden, help_text.lower())


if __name__ == "__main__":
    unittest.main()
