"""Chart-workbench demo runtime (test asset): real ProductRuntime + realistic market fixture.

用途：为真实浏览器截图 / 人工验收提供一段有 K 线、有 order+fill、有 semantic marker 的运行。
不是产品代码；真实启动仍走同一个 composition root (`runtime.assembly.ProductRuntime`)。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from portfolio.types import Side
from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from risk.types import OrderProposal
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile

REPO = pathlib.Path(__file__).resolve().parents[2]
HISTORY_CAPACITY = 6000
MAX_POINTS = 300


def build_runtime(*, mode: str, port: int, event_store: str, registry_dir: str) -> ProductRuntime:
    values = json.loads((REPO / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
    values["projection.history_capacity"] = HISTORY_CAPACITY
    values["projection.max_points"] = MAX_POINTS
    values["projection.window_ms"] = 3_600_000
    entries = tuple(ConfigEntry(name=str(key), source=ConfigSource.FILE, value=Fact.of(value))
                    for key, value in values.items())
    return ProductRuntime(profile=RuntimeProfile(
        symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode(mode.upper()), host="127.0.0.1",
        port=port, run_registry_dir=registry_dir,
        feed=FeedProfile(event_store=event_store, window_ms=3_600_000, bucket_ms=1_000,
                         max_points=MAX_POINTS, price_levels=5, history_capacity=HISTORY_CAPACITY,
                         view_depth=10)))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "paper"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8840
    event_store = sys.argv[3] if len(sys.argv) > 3 else "/tmp/probex_ui/events.jsonl"
    registry_dir = sys.argv[4] if len(sys.argv) > 4 else "/tmp/probex_ui/runs"
    runtime = build_runtime(mode=mode, port=port, event_store=event_store, registry_dir=registry_dir)
    runtime.start()
    provider = runtime._feed_provider  # noqa: SLF001
    deadline = time.time() + 20
    while time.time() < deadline and not provider.stats.get("completed"):
        time.sleep(0.05)
    smoke = None
    if mode.lower() == "paper":
        now = int(runtime.profile.clock())
        runtime._accounting.update_mark_price("BTCUSDT", 60_000.0, timestamp=now)  # noqa: SLF001
        submitted = runtime._execution.submit(                                     # noqa: SLF001
            OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=0.002, price=60_000.0,
                          post_only=True), now_ms=now)
        client_order_id = submitted.order.client_order_id
        runtime._execution.manager.adapter.broker.fill(client_order_id, quantity=0.002, price=60_000.0,
                                                timestamp=now)                      # noqa: SLF001
        runtime._execution.poll(now_ms=now)                                        # noqa: SLF001
        smoke = {"order": client_order_id, "submitted": submitted.submitted}
    runtime.create_server()
    print(json.dumps({"event": "ready", "mode": mode.upper(), "run_id": runtime.run_id,
                      "url": runtime.server_url(), "smoke": smoke}), flush=True)
    runtime.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
