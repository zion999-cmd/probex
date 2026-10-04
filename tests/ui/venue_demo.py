"""P0001.15 venue demo runtime（测试资产）：真实 ProductRuntime + 含 MARK_PRICE 的 fixture。

用于真实浏览器验收（acceptance F）：真实 instrument identity + 正式 MARK reference price +
自然产生的 decision/order（不是手工 smoke order）+ 两个 connector 的独立 health。

不是产品代码；启动走同一个 composition root（`runtime.assembly.ProductRuntime`）。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile
from tests.integration.test_venue_integration_e2e import MAKER_VALUES, RISK_VALUES
from tests.ui.market_fixture import write_market_store

REPO = pathlib.Path(__file__).resolve().parents[2]
MARK_PRICE = 60_000.0
HISTORY_CAPACITY = 6000
MAX_POINTS = 300


def build_runtime(*, mode: str, port: int, event_store: str, registry_dir: str) -> ProductRuntime:
    values: dict[str, object] = json.loads((REPO / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
    values.update(MAKER_VALUES)          # 测试值：让 MakerPolicy 自然产出报价
    values.update(RISK_VALUES)
    values["projection.history_capacity"] = HISTORY_CAPACITY
    values["projection.max_points"] = MAX_POINTS
    values["projection.window_ms"] = 3_600_000
    entries = tuple(ConfigEntry(name=str(key), source=ConfigSource.FILE, value=Fact.of(value))
                    for key, value in values.items())
    runtime = ProductRuntime(profile=RuntimeProfile(
        symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode(mode.upper()), host="127.0.0.1",
        port=port, run_registry_dir=registry_dir,
        feed=FeedProfile(event_store=event_store, window_ms=3_600_000, bucket_ms=1_000,
                         max_points=MAX_POINTS, price_levels=5, history_capacity=HISTORY_CAPACITY,
                         view_depth=10)))
    from tests.fakes import FakeProvider

    runtime.attach_prediction_provider(FakeProvider(), timeout_ms=1_000, ttl_ms=120_000)
    return runtime


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "paper"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8840
    event_store = sys.argv[3] if len(sys.argv) > 3 else "/tmp/probex_venue/events.jsonl"
    registry_dir = sys.argv[4] if len(sys.argv) > 4 else "/tmp/probex_venue/runs"
    pathlib.Path(event_store).parent.mkdir(parents=True, exist_ok=True)
    write_market_store(pathlib.Path(event_store), hours=3, mark_price=MARK_PRICE)

    runtime = build_runtime(mode=mode, port=port, event_store=event_store, registry_dir=registry_dir)
    runtime.start()
    loop = runtime._decision_loop                                                   # noqa: SLF001
    deadline = time.time() + 30
    while time.time() < deadline and loop.status.submits < 2:
        time.sleep(0.05)
    snapshot = runtime.service.snapshot()
    if loop.status.submits < 2:
        print(f"WARNING: no natural PAPER orders yet: {loop.status.__dict__}", file=sys.stderr)
    output = {
        "mode": mode, "url": f"http://127.0.0.1:{port}",
        "instrument": snapshot.instrument.instrument_id.value if snapshot.instrument.instrument_id.known else None,
        "product_type": snapshot.instrument.product_type.value if snapshot.instrument.product_type.known else None,
        "reference_price": snapshot.reference_price.price.value if snapshot.reference_price.known.known
        and snapshot.reference_price.known.value else None,
        "reference_source": snapshot.reference_price.source.value if snapshot.reference_price.source.known else None,
        "orders": [order.client_order_id for order in snapshot.execution.active_orders],
        "decision_ids": [order.decision_id.value for order in snapshot.execution.active_orders
                         if order.decision_id.known],
        "market_connector": snapshot.market_connector_health.connection_state.value
        if snapshot.market_connector_health.connection_state.known else None,
        "private_connector": snapshot.private_connector_health.connection_state.value
        if snapshot.private_connector_health.connection_state.known else None,
        "submits": loop.status.submits, "risk_rejects": loop.status.risk_rejects,
    }
    pathlib.Path(registry_dir).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(registry_dir).parent / "venue_demo.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False), flush=True)
    server = runtime.create_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
