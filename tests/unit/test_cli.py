"""P0001.10.3 Product CLI 单测：只读、机器优先、稳定退出码、stdout/stderr 分离。"""

from __future__ import annotations

import io
import json
import threading
import unittest

from api.server import create_server
from cli.main import (
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_UNKNOWN,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    main,
)
from product.types import Fact, RuntimeIdentity, RuntimeMode
from reports import build_run_summary
from tests.unit.test_product_snapshot import service


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        summary = build_run_summary(
            identity=RuntimeIdentity(mode=RuntimeMode.PAPER, environment="testnet", venue="binance",
                                     symbol="BTCUSDT", runtime_id="rt-1", started_at=1,
                                     data_timestamp=Fact.of(2)),
            run_id="rt-1", started_at=1,
            telemetry=(__import__("tests.unit.test_report_builder", fromlist=["telemetry"]).telemetry(
                submit=1, ts=1_000),),
            final_position=0.0, realized_pnl=-0.25)
        cls.server = create_server(service(run_summary=lambda: summary), host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_every_read_command_works_in_json_mode(self) -> None:
        for command in ("status", "snapshot", "market", "prediction", "strategy", "risk", "orders",
                        "portfolio", "readiness", "evidence"):
            with self.subTest(command=command):
                code, out, err = run_cli("--api-url", self.api, command, "--json")
                self.assertEqual(code, EXIT_OK)
                self.assertEqual(err, "")
                payload = json.loads(out)
                self.assertTrue(payload)

    def test_stdout_is_data_and_stderr_is_diagnostics(self) -> None:
        code, out, err = run_cli("--api-url", self.api, "market")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("best_bid", out)
        self.assertEqual(err, "")

    def test_unavailable_api_returns_exit_10(self) -> None:
        code, out, err = run_cli("--api-url", "http://127.0.0.1:9", "status")
        self.assertEqual(code, EXIT_UNAVAILABLE)
        self.assertEqual(out, "")
        self.assertIn("unavailable", err)

    def test_unknown_evidence_returns_exit_11(self) -> None:
        class Server(self.server.__class__):  # noqa: N801 - 仅用于本用例
            pass

        server = create_server(service(prediction=lambda: None), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = f"http://127.0.0.1:{server.server_address[1]}"
            code, out, err = run_cli("--api-url", api, "prediction")
            self.assertEqual(code, EXIT_UNKNOWN)
            self.assertIn("UNKNOWN", out)
            self.assertIn("insufficient evidence", err)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        del Server

    def test_blocked_readiness_with_strict_returns_exit_20(self) -> None:
        code, out, err = run_cli("--api-url", self.api, "readiness", "--strict")
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("PRIVATE_LATENCY_UNOBSERVED", out)
        self.assertIn("blocked", err)

    def test_explain_finds_the_decision_trace(self) -> None:
        code, out, err = run_cli("--api-url", self.api, "explain", "decision", "pred-1", "--json")
        self.assertEqual(code, EXIT_OK)
        trace = json.loads(out)["trace"]
        self.assertTrue(any(entry["stage"] == "maker_decision" for entry in trace))

    def test_explain_unknown_identifier_returns_exit_11(self) -> None:
        code, out, err = run_cli("--api-url", self.api, "explain", "order", "nope")
        self.assertEqual(code, EXIT_UNKNOWN)
        self.assertIn("no evidence entry", err)

    def test_report_run_binds_identity(self) -> None:
        code, out, err = run_cli("--api-url", self.api, "report", "run", "rt-1")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("run_id: rt-1", out)
        self.assertIn("format=markdown", out)

    def test_report_for_unknown_run_returns_exit_11(self) -> None:
        code, out, err = run_cli("--api-url", self.api, "report", "run", "does-not-exist")
        self.assertEqual(code, EXIT_UNKNOWN)

    def test_no_write_commands_exist(self) -> None:
        """CLI 不提供任何交易/参数写入口（命令表里不存在，且解析失败返回 2）。"""
        from cli.main import build_parser

        parser = build_parser()
        subparsers = [action for action in parser._actions if action.dest == "command"]  # noqa: SLF001
        choices = set(subparsers[0].choices or {})
        for command in ("buy", "sell", "order", "set-risk", "set-leverage", "cancel", "position"):
            with self.subTest(command=command):
                self.assertNotIn(command, choices)
                code, out, err = run_cli("--api-url", self.api, command)
                self.assertEqual(code, EXIT_USAGE)
                self.assertEqual(out, "")

    def test_missing_command_prints_help_to_stderr_and_exits_2(self) -> None:
        code, out, err = run_cli("--api-url", self.api)
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertIn("usage", err.lower())

    def test_module_entry_point_is_wired(self) -> None:
        import pathlib

        entry = pathlib.Path("cli/__main__.py")
        self.assertTrue(entry.exists())
        self.assertIn("run()", entry.read_text(encoding="utf-8"))
