"""P0001.17 §2/§16B：Surface 状态注入 harness（**测试资产**，不改产品行为）。

用途：真实启动应用，但让指定 provider **真实失败**，从而在浏览器里逐页验证
`Loading / Error(+恢复入口) / Empty / UNKNOWN(+reason)` 状态。

两个模式：

- `--mode=fault`：市场事实 provider 抛错 ⇒ `/api/v1/market` 等 500/503，页面必须显示 error + 恢复入口；
- `--mode=empty`：事件存储为空 ⇒ 没有市场事实/订单/成交 ⇒ 页面必须显示 UNKNOWN + reason（不是 0/空冒充健康）。

用法：python3 tests/ui/fault_server.py <port> <mode>
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile

REPO = pathlib.Path(__file__).resolve().parents[2]


class InjectedFailure(RuntimeError):
    """注入的 provider 失败（测试专用）。"""


def _build(*, port: int, mode: str) -> ProductRuntime:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"probex-fault-{mode}-"))
    store = tmp / "events.jsonl"
    if mode == "empty":
        store.write_text("", encoding="utf-8")          # 真实空事件存储（不是 mock 数据）
    else:
        from tests.ui.market_fixture import write_market_store

        write_market_store(store, hours=1, mark_price=60_000.0)
    values: dict[str, object] = json.loads((REPO / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
    entries = tuple(ConfigEntry(name=str(k), source=ConfigSource.FILE, value=Fact.of(v))
                    for k, v in values.items())
    runtime = ProductRuntime(profile=RuntimeProfile(
        symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode.PAPER, host="127.0.0.1", port=port,
        run_registry_dir=str(tmp / "runs"),
        feed=FeedProfile(event_store=str(store), window_ms=3_600_000, bucket_ms=1_000,
                         max_points=200, price_levels=5, history_capacity=500, view_depth=10)))
    if mode == "fault":
        # 真实失败路径：provider 抛错 ⇒ API 5xx（页面必须显示 error + 恢复入口，而不是假装健康）
        runtime.service.market_state = lambda: (_ for _ in ()).throw(InjectedFailure("market facts provider failed"))
        runtime.service.prediction = lambda: (_ for _ in ()).throw(InjectedFailure("prediction provider failed"))
    return runtime


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8902
    mode = sys.argv[2] if len(sys.argv) > 2 else "fault"
    runtime = _build(port=port, mode=mode)
    runtime.start()
    print(json.dumps({"mode": mode, "url": f"http://127.0.0.1:{port}", "run_id": runtime.run_id}), flush=True)
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
