"""P0001.16 真实 TESTNET 只读产品服务（截图/人工验收用；**不下任何单**）。

启动真实 TESTNET 组合（public 行情 + private stream + readiness），把 `ProductService` 暴露为
只读 HTTP 服务（与 REPLAY/PAPER 同一个产品读模型），用于真实浏览器截图。

用法：python3 tests/ui/testnet_demo.py <port> <out-json>
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from api.server import create_server
from runtime.testnet import build_testnet_product_service, build_testnet_stack
from tests.acceptance.testnet_acceptance_run import build_config


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8890
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/probex_p116/testnet_demo.json"
    config, stack = build_config(acceptance_enabled=False), None
    config, stack = config, build_testnet_stack(config=config)  # type: ignore[assignment]
    stack.start()
    try:
        stack.start_market_window(timeout_s=25)
        stack.pump_market(seconds=25)
        stack.pump_private(seconds=15)
        stack.refresh_account_facts()
        stack.private.refresh_clock_calibration()
        readiness = stack.collect_readiness()
        service = build_testnet_product_service(stack)
        snapshot = service.snapshot()
        summary = {
            "mode": str(snapshot.runtime.mode.value), "venue": snapshot.venue.venue_id.value,
            "environment": snapshot.venue.environment.value,
            "instrument": snapshot.instrument.instrument_id.value,
            "reference_price": snapshot.reference_price.price.value if snapshot.reference_price.known.known
            and snapshot.reference_price.known.value else None,
            "reference_price_source": snapshot.reference_price.source.value
            if snapshot.reference_price.source.known else None,
            "position": stack.position_qty(),
            "position_local": snapshot.portfolio.position_qty.value if snapshot.portfolio.position_qty.known else None,
            "active_orders": [o.client_order_id for o in snapshot.execution.active_orders],
            "private_connector": snapshot.private_connector_health.connection_state.value
            if snapshot.private_connector_health.connection_state.known else None,
            "market_connector": snapshot.market_connector_health.connection_state.value
            if snapshot.market_connector_health.connection_state.known else None,
            "readiness": readiness.status.value,
            "readiness_reasons": [r.value for r in (readiness.reasons or ())],
            "last_submit": snapshot.execution.last_submit_reason.value
            if snapshot.execution.last_submit_reason.known else None,
        }
        pathlib.Path(out).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        server = create_server(service, host="127.0.0.1", port=port)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
