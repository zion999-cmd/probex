"""P0001.11.2 集成：REPLAY / PAPER / TESTNET 自动登记；LIVE 接线结构验证。"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from api.server import create_server
from execution.adapters.paper import PaperBroker
from execution.manager import OrderManager
from execution.types import Order, OrderStatus
from market.events.types import Venue
from market.replay.source import ReplaySource
from portfolio.types import Side
from product.provenance import ConfigEntry, ConfigSource, build_config_snapshot
from product.types import Fact, RuntimeMode
from reports.types import RunStatus
from runtime.session import RuntimeSession, SessionSummaryFacts
from runtime.wiring import SessionHost, open_live_session, open_paper_session, open_replay_session, \
    open_testnet_session
from storage.events.reader import JsonlEventReader
from storage.run_registry import JsonRunRegistry
from tests import scenarios
from tests.orchestration_support import NOW, OrchestrationStack
from tests.support import market_book, replay_into, write_store
from tests.unit.test_product_snapshot import service

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ProductionWiringIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probex-wiring-int-"))
        self.registry = JsonRunRegistry(self.tmp / "runs")
        self.store = self.tmp / "events.jsonl"
        self.now = [1_000]
        self.config = build_config_snapshot(
            config_id="cfg-wiring",
            entries=[ConfigEntry(name="symbol", source=ConfigSource.CONSTRUCTOR, value=Fact.of("BTCUSDT")),
                     ConfigEntry(name="mode", source=ConfigSource.CLI, value=Fact.of("wiring"))],
            created_at=1_000)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clock(self) -> int:
        return self.now[0]

    def session(self, mode: RuntimeMode, runtime_id: str) -> RuntimeSession:
        return RuntimeSession(mode=mode, environment="local", venue="binance", symbol="BTCUSDT",
                              registry=self.registry, clock=self.clock, runtime_id=runtime_id,
                              config=self.config)

    def replay_feed(self):
        events = scenarios.reference_events()
        write_store(self.store, events)
        return events, ReplaySource(JsonlEventReader(self.store))

    def api_base(self):
        server = create_server(service(config_snapshot=lambda: self.config, run_registry=lambda: self.registry),
                               host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    # ------------------------------------------------------------------ REPLAY

    def test_replay_run_registers_automatically(self) -> None:
        events, source = self.replay_feed()
        book = market_book()
        host = open_replay_session(mode=RuntimeMode.REPLAY, environment="local", venue="binance",
                                   symbol="BTCUSDT", session=self.session(RuntimeMode.REPLAY, "replay-wire"),
                                   facts_provider=lambda: SessionSummaryFacts(
                                       data_range=(events[0].exchange_ts, events[-1].exchange_ts)))
        host.start()
        self.now[0] = 30_000
        consumed = host.run_feed(source.iter_events(), step=lambda event: replay_into(book, [event]))
        record = host.finish()

        self.assertEqual(consumed, len(events))
        self.assertEqual(record.status, RunStatus.COMPLETED)
        self.assertEqual(record.summary["run"]["config_id"]["value"], "cfg-wiring")

        server, thread, base = self.api_base()
        try:
            with OPENER.open(f"{base}/api/v1/runs", timeout=5) as response:
                runs = {run["run_id"]: run["status"] for run in json.loads(response.read().decode())["runs"]}
            self.assertEqual(runs["replay-wire"], "COMPLETED")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    # ------------------------------------------------------------------ PAPER

    def test_paper_run_registers_automatically_with_real_paper_broker(self) -> None:
        broker = PaperBroker()
        broker.update_book("BTCUSDT", best_bid=59_999.0, best_ask=60_001.0)
        manager = OrderManager(tracker=__import__("execution.tracker", fromlist=["OrderTracker"]).OrderTracker(
            session_id="paper-wire"), adapter=broker)
        host = open_paper_session(mode=RuntimeMode.PAPER, environment="local", venue="binance",
                                  symbol="BTCUSDT", session=self.session(RuntimeMode.PAPER, "paper-wire"),
                                  facts_provider=lambda: SessionSummaryFacts(fills=len(broker.poll())))
        host.start()
        order = Order(client_order_id="probex-paper-1", venue=Venue.BINANCE, symbol="BTCUSDT", side=Side.BUY,
                      price=59_000.0, quantity=0.001, status=OrderStatus.PENDING_CREATE,
                      created_at=NOW, updated_at=NOW, post_only=True)
        self.now[0] = 20_000
        events = broker.submit(order)
        manager.on_events(events)
        record = host.finish()

        self.assertEqual(record.status, RunStatus.COMPLETED)
        # 真实 paper 执行路径确实产生了执行事件（order lifecycle 由既有 Owner 负责）
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(type(events[0]).__name__, "OrderAccepted")
        self.assertEqual(self.registry.load("paper-wire").status, RunStatus.COMPLETED)
        del manager

    # ------------------------------------------------------------------ TESTNET

    def test_testnet_session_registers_and_stop_failure_stays_incomplete(self) -> None:
        def refusing_stop() -> None:
            raise RuntimeError("engine stop refused (simulated)")

        host = open_testnet_session(mode=RuntimeMode.TESTNET, environment="local", venue="binance",
                                    symbol="BTCUSDT", session=self.session(RuntimeMode.TESTNET, "testnet-wire"),
                                    stop_hook=refusing_stop)
        host.start()
        self.now[0] = 40_000
        with self.assertRaises(RuntimeError):
            host.finish()

        self.assertEqual(self.registry.load("testnet-wire").status, RunStatus.INCOMPLETE)

    def test_testnet_session_registers_on_graceful_stop(self) -> None:
        stopped: list[str] = []
        host = open_testnet_session(mode=RuntimeMode.TESTNET, environment="local", venue="binance",
                                    symbol="BTCUSDT", session=self.session(RuntimeMode.TESTNET, "testnet-ok"),
                                    stop_hook=lambda: stopped.append("owner-stop"),
                                    facts_provider=lambda: SessionSummaryFacts(final_position=0.0))
        host.start()
        host.run_feed([], step=None)
        self.now[0] = 50_000
        record = host.finish()

        self.assertEqual(stopped, ["owner-stop"])
        self.assertEqual(record.status, RunStatus.COMPLETED)

    # ------------------------------------------------------------------ LIVE（结构验证，无实盘）

    def test_live_wiring_exists_and_uses_the_original_stop_owner(self) -> None:
        """SC-4：live 的 stop 仍是原 Owner（orchestrator.stop）；RuntimeSession 只在其完成后 finalize。"""
        stack = OrchestrationStack.build()
        host = open_live_session(mode=RuntimeMode.LIVE, environment="local", venue="binance",
                                 symbol="BTCUSDT", session=self.session(RuntimeMode.LIVE, "live-wire"),
                                 stop_hook=lambda: stack.orchestrator.stop(position_qty=0.0))
        host.start()
        self.now[0] = 60_000
        record = host.finish()

        self.assertEqual(record.status, RunStatus.COMPLETED)
        self.assertEqual(stack.orchestrator.state.value, "STOPPED")
        self.assertIs(stack.orchestrator.state, __import__("live.orchestrator", fromlist=["OrchestratorState"]).OrchestratorState.STOPPED)

    def test_wiring_does_not_grant_write_ability_to_the_runtime_layer(self) -> None:
        source = Path(__import__("runtime.wiring", fromlist=["x"]).__file__).read_text(encoding="utf-8")
        for forbidden in ("BinanceExecutionAdapter", "submit_post_only_limit", "place_order", "set_leverage"):
            self.assertNotIn(forbidden, source)
