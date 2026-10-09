"""P0001.17 本地 PAPER 演示 runtime（测试资产）：真实本地交易闭环 + 产品 UI 服务。

与 `demo_runtime.py`（手工 fill）不同，这里跑的是**真实本地闭环**：

    Replay 市场数据 → MarketState → LOCAL_TRIAL prediction（试验） → MakerPolicy decision
    → RiskGate → PAPER 下单 → 既有 `SimulatedVenue` 事件级成交 → OrderTracker
    → FillLedger / AccountingCore → Product Read Model（UI）

用法：python3 tests/ui/local_paper_demo.py <port> <out-json> [hours]
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

REPO = pathlib.Path(__file__).resolve().parents[2]
HISTORY_CAPACITY = 6000
MAX_POINTS = 300


def build_runtime(*, port: int, event_store: str, registry_dir: str) -> ProductRuntime:
    values: dict[str, object] = json.loads((REPO / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
    values["projection.history_capacity"] = HISTORY_CAPACITY
    values["projection.max_points"] = MAX_POINTS
    values["projection.window_ms"] = 3_600_000
    entries = tuple(ConfigEntry(name=str(key), source=ConfigSource.FILE, value=Fact.of(value))
                    for key, value in values.items())
    return ProductRuntime(profile=RuntimeProfile(
        symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode.PAPER, host="127.0.0.1", port=port,
        run_registry_dir=registry_dir,
        feed=FeedProfile(event_store=event_store, window_ms=3_600_000, bucket_ms=1_000,
                         max_points=MAX_POINTS, price_levels=5, history_capacity=HISTORY_CAPACITY,
                         view_depth=10)))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8892
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/probex_p117/local_paper_demo.json"
    hours = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    store = pathlib.Path(out).parent / "events.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    from tests.ui.market_fixture import write_market_store

    write_market_store(store, hours=hours, mark_price=60_000.0)

    runtime = build_runtime(port=port, event_store=str(store), registry_dir=str(pathlib.Path(out).parent / "runs"))
    runtime.start()
    loop = runtime._decision_loop                                                # noqa: SLF001
    deadline = time.time() + 90
    accounting = runtime._execution.accounting                                   # noqa: SLF001
    while time.time() < deadline and not accounting.fills.fills:
        time.sleep(0.25)
    snapshot = runtime.service.snapshot()
    summary = {
        "mode": snapshot.runtime.mode.value,
        "prediction_provider": snapshot.prediction.provider.value if snapshot.prediction.provider.known else None,
        "prediction_is_local_trial": snapshot.prediction.is_local_trial.value
        if snapshot.prediction.is_local_trial.known else None,
        "decisions": loop.status.decisions, "submits": loop.status.submits,
        "orders": [(o.client_order_id, o.status.value, o.filled_quantity) for o in runtime._tracker_owner.orders],  # noqa: SLF001
        "fills": len(accounting.fills.fills),
        "position_qty": accounting.position("BTCUSDT").qty,
        "equity": accounting.equity(),
        "realized_pnl": accounting.realized_trade_pnl,
        "decision_ids": [o.correlation.decision_id for o in runtime._tracker_owner.orders  # noqa: SLF001
                         if o.correlation is not None],
        "url": f"http://127.0.0.1:{port}",
    }
    pathlib.Path(out).write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
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
