"""Closure polish 真实 E2E（F1/F2/F3/F4/F5/F7）：真实 PAPER runtime + HTTP + Node 页面渲染。"""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from product.provenance import ConfigEntry, ConfigSource
from product.service import ProductService
from product.types import Fact, RuntimeIdentity, RuntimeMode
from portfolio.types import Side
from risk.types import OrderProposal
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile
from tests import scenarios
from tests.support import write_store
from api.server import create_server

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
RENDERER = PROJECT_ROOT / "tests" / "ui" / "render_page.mjs"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def render_page(base: str, module: str, rest: list) -> dict:
    result = subprocess.run(["node", str(RENDERER), base, module, json.dumps(rest)],
                            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"node renderer produced no output: {result.stderr[-400:]}")
    return json.loads(lines[-1])


class ReadModelPolishE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="probex-polish-"))
        self.server = None
        self.thread = None
        self.runtime: ProductRuntime | None = None

    def tearDown(self) -> None:
        if self.runtime is not None:
            try:
                self.runtime.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def _runtime(self) -> ProductRuntime:
        store = self.tmp / "events.jsonl"
        write_store(store, scenarios.reference_events())
        values = json.loads((PROJECT_ROOT / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
        # 本套件验证"手工注入 fill"的读模型路径 ⇒ 显式关闭事件级模拟（P0001.17）
        values["simulation.enabled"] = False
        entries = tuple(ConfigEntry(name=str(k), source=ConfigSource.FILE, value=Fact.of(v))
                        for k, v in values.items())
        return ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode.PAPER,
            run_registry_dir=str(self.tmp / "runs"),
            feed=FeedProfile(event_store=str(store), window_ms=600_000, bucket_ms=1_000,
                             max_points=200, price_levels=5, history_capacity=500, view_depth=10)))

    def _serve(self, runtime: ProductRuntime) -> str:
        self.server = runtime.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return runtime.server_url()

    def _get(self, base: str, path: str) -> tuple[int, dict]:
        try:
            with OPENER.open(f"{base}{path}", timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_real_paper_run_closes_f1_f2_f3_f4_f5_f7(self) -> None:
        runtime = self._runtime()
        self.runtime = runtime
        runtime.start()
        provider = runtime._feed_provider  # noqa: SLF001
        deadline = time.time() + 5
        while time.time() < deadline and not provider.stats.get("completed"):
            time.sleep(0.02)
        mark = int(runtime.profile.clock())
        runtime._accounting.update_mark_price("BTCUSDT", 59_000.0, timestamp=mark)  # noqa: SLF001

        # ---------------- F1/F2 before the order ----------------
        snap = runtime.service.snapshot()
        self.assertTrue(snap.portfolio.position_qty.known)                # known flat
        self.assertEqual(snap.portfolio.position_qty.value, 0.0)
        self.assertIsNot(snap.portfolio.position_qty.value, False)
        self.assertTrue(snap.market.healthy.known)
        self.assertTrue(snap.market.healthy.value)                        # book_health=healthy
        self.assertEqual(snap.market.book_health.value, "healthy")
        self.assertTrue(snap.health.market_healthy.value)                 # same projection

        base = self._serve(runtime)

        # ---------------- F3/F7 via real page render ----------------
        monitor = render_page(base, "ui/pages/monitor/page.js", [])
        self.assertTrue(monitor["ok"], monitor)
        self.assertIn("flat (0)", monitor["html"])
        self.assertNotIn("[object Object]", monitor["html"])

        system = render_page(base, "ui/pages/system/page.js", ["ops"])
        self.assertTrue(system["ok"], system)
        self.assertNotIn("[object Object]", system["html"])
        self.assertIn("bind_host", system["html"])                        # readable posture summary
        self.assertIn("bounded", system["html"])

        activity = render_page(base, "ui/pages/activity/page.js", [])
        self.assertTrue(activity["ok"], activity)
        self.assertIn("ABSENT", activity["html"])                         # F7
        self.assertNotIn("UNKNOWN UNKNOWN", activity["html"])

        # ---------------- real order + real fill ----------------
        proposal = OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.001, price=59_000.0,
                                 post_only=True)
        submitted = runtime._execution.submit(proposal, now_ms=mark)      # noqa: SLF001
        self.assertTrue(submitted.submitted, submitted)
        client_order_id = submitted.order.client_order_id
        # P0001.15 §9：唯一的 PaperBroker 由统一 execution connector 持有（feed 不再有实例）
        adapter = runtime._execution.manager.adapter                      # noqa: SLF001
        adapter.broker.fill(client_order_id, quantity=0.001, price=59_000.0, timestamp=mark)
        runtime._execution.poll(now_ms=mark)                              # noqa: SLF001

        snap = runtime.service.snapshot()
        self.assertTrue(snap.portfolio.position_qty.known)
        self.assertEqual(snap.portfolio.position_qty.value, 0.001)

        # ---------------- F5 raw facts (order + fill) ----------------
        status, order_fact = self._get(base, f"/api/v1/facts/order/{client_order_id}")
        self.assertEqual(status, 200, order_fact)
        order_fields = dict(order_fact["fact"]["facts"])
        self.assertEqual(order_fields["client_order_id"]["value"], client_order_id)
        self.assertEqual(order_fields["status"]["value"], "FILLED")
        status, fill_fact = self._get(base, f"/api/v1/facts/fill/{client_order_id}")
        self.assertEqual(status, 200, fill_fact)
        fill_fields = dict(fill_fact["fact"]["facts"])
        self.assertEqual(fill_fields["client_order_id"]["value"], client_order_id)
        self.assertTrue(fill_fields["trade_id"]["value"])
        self.assertNotIn("signature", json.dumps(fill_fact).lower())

        # ---------------- F4 durable run summary ----------------
        run_id = runtime.run_id
        runtime.stop()
        self.server.shutdown()
        self.server.server_close()
        base = self._serve(runtime)
        status, summary = self._get(base, f"/api/v1/reports/run-summary?run={run_id}")
        self.assertEqual(status, 200, summary)
        metrics = summary["run_summary"]["metrics"]
        self.assertTrue(metrics["realized_pnl"]["known"])
        self.assertTrue(metrics["fees"]["known"])
        self.assertTrue(metrics["run_mdd"]["known"])                      # real equity samples
        self.assertEqual(summary["run_summary"]["order_counts"], {"FILLED": 1})
        self.assertTrue(summary["run_summary"]["final_position"]["known"])
        status, _ = self._get(base, "/api/v1/reports/run-summary?run=does-not-exist")
        self.assertEqual(status, 404)

    def test_no_page_renders_object_object(self) -> None:
        """F3/F7 守卫：所有页面在真实数据下渲染都不得出现 `[object Object]`，且必须成功执行。"""
        runtime = self._runtime()
        self.runtime = runtime
        runtime.start()
        provider = runtime._feed_provider  # noqa: SLF001
        deadline = time.time() + 5
        while time.time() < deadline and not provider.stats.get("completed"):
            time.sleep(0.02)
        mark = int(runtime.profile.clock())
        runtime._accounting.update_mark_price("BTCUSDT", 59_000.0, timestamp=mark)  # noqa: SLF001
        submitted = runtime._execution.submit(                          # noqa: SLF001
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.001, price=59_000.0,
                          post_only=True), now_ms=mark)
        client_order_id = submitted.order.client_order_id
        runtime._execution.manager.adapter.broker.fill(client_order_id, quantity=0.001, price=59_000.0,
                                                timestamp=mark)          # noqa: SLF001
        runtime._execution.poll(now_ms=mark)                            # noqa: SLF001
        base = self._serve(runtime)
        run_id = runtime.run_id

        pages = [
            ("ui/pages/monitor/page.js", []), ("ui/pages/market/page.js", []),
            ("ui/pages/activity/page.js", []), ("ui/pages/performance/page.js", []),
            ("ui/pages/system/page.js", []),
            ("ui/pages/system/page.js", ["ops"]), ("ui/pages/system/page.js", ["execution"]),
            ("ui/pages/system/page.js", ["configuration"]), ("ui/pages/system/page.js", ["risk"]),
            ("ui/pages/system/page.js", ["readiness"]),
            ("ui/pages/system/page.js", ["capabilities"]),
            ("ui/pages/overview/page.js", []), ("ui/pages/portfolio/page.js", []),
            ("ui/pages/prediction/page.js", []), ("ui/pages/strategy/page.js", []),
            ("ui/pages/orders/page.js", []), ("ui/pages/evidence/page.js", ["order", client_order_id]),
            ("ui/pages/runs/page.js", []), ("ui/pages/metrics/page.js", []),
            ("ui/pages/performance/page.js", [run_id]),
            ("ui/pages/activity/page.js", ["run", run_id]),
            ("ui/pages/market/page.js", ["run-review", run_id]),
            ("ui/pages/market/page.js", ["live", "1700000000110"]),
        ]
        for module, rest in pages:
            with self.subTest(module=module, rest=rest):
                result = render_page(base, module, rest)
                self.assertTrue(result["ok"], result)
                self.assertNotIn("[object Object]", result["html"])

    def test_raw_fact_provider_unwired_is_503_not_404(self) -> None:
        service = ProductService(identity=RuntimeIdentity(
            mode=RuntimeMode.REPLAY, environment="local", venue="binance", symbol="BTCUSDT",
            runtime_id="rt-bare", started_at=1, data_timestamp=Fact.unknown("x")))
        server = create_server(service, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            status, payload = self._get(base, "/api/v1/facts/order/probex-1")
            self.assertEqual(status, 503, payload)
            self.assertEqual(payload["error"], "raw_fact_provider_unavailable")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
