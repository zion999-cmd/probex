"""P0001.9.4.1 SC-16 authenticated live acceptance（opt-in，需要凭据）：真实 `/fapi/v1/income`。

```bash
export BINANCE_API_KEY=... BINANCE_API_SECRET=... PROBEX_LIVE_PRIVATE=1
export PROBEX_LIVE_INCOME_PAGE_LIMIT=1000         # 可选（默认 1000）
export PROBEX_LIVE_INCOME_MAX_PAGES=20            # **必填**：分页安全上限（无默认业务值）
python3 -m unittest -v tests.live.test_binance_income_live
```

测试网端点通过 `PROBEX_LIVE_REST_BASE` / `PROBEX_LIVE_WS_HOST` 覆盖（见 recovery live 测试）。

安全：只读；凭据只从环境变量读取；报告不含凭据/签名。
"""

from __future__ import annotations

import json
import os
import time
import unittest

from connectors.binance.private.income import (
    MAX_PAGE_LIMIT,
    IncomeClass,
    fetch_income_history,
)
from connectors.binance.private.rest import PrivateRestClient, UrllibRestFetcher
from market.events.types import Milliseconds
from risk.snapshot import utc_day_start_ms
from tests.live.test_binance_recovery_live import rest_base
from tests.live_support import BINANCE_SYMBOL

LIVE_FLAG = "PROBEX_LIVE_PRIVATE"
MAX_PAGES_ENV = "PROBEX_LIVE_INCOME_MAX_PAGES"
PAGE_LIMIT_ENV = "PROBEX_LIVE_INCOME_PAGE_LIMIT"


def max_pages_from_env() -> int:
    """分页上限必须显式给出（不猜业务/安全数值）。"""
    value = os.environ.get(MAX_PAGES_ENV)
    if value in (None, ""):
        raise RuntimeError(f"{MAX_PAGES_ENV} must be provided explicitly (page-count safety bound)")
    return int(value)


def build_income_client() -> PrivateRestClient:
    from connectors.binance.private.auth import ApiCredentials

    return PrivateRestClient(
        credentials=ApiCredentials.from_env(),
        fetcher=UrllibRestFetcher(),
        base_url=rest_base(),
        recv_window_ms=5_000,
        timeout_s=15.0,
        clock=lambda: int(time.time() * 1000),  # noqa: E731
    )


def income_report(*, facts, day_start_ts: Milliseconds, cutoff_ts: Milliseconds, elapsed_ms: int) -> dict:
    """构造可审计报告（计数与聚合，不含凭据）。"""
    by_type: dict[str, int] = {}
    for row in facts.rows:
        by_type[row.income_type] = by_type.get(row.income_type, 0) + 1
    return {
        "environment": {
            "rest_base": rest_base(),
            "symbol_scope": "ACCOUNT_LEVEL（不传 symbol）",
            "note": "只读 /fapi/v1/income；无下单/撤单",
        },
        "window": {
            "day_start_ts": day_start_ts,
            "cutoff_ts": cutoff_ts,
            "span_ms": cutoff_ts - day_start_ts,
            "utc_day": True,
        },
        "coverage": {
            "complete": facts.coverage.complete,
            "reason": facts.coverage.reason,
            "pages": facts.coverage.pages,
            "rows": facts.coverage.rows,
            "start_ts": facts.coverage.start_ts,
            "end_ts": facts.coverage.end_ts,
            "duplicates_dropped": facts.duplicates_dropped,
        },
        "classification": {
            "trading_rows": len(facts.by_class(IncomeClass.TRADING)),
            "non_trading_rows": len(facts.by_class(IncomeClass.NON_TRADING)),
            "unclassified_rows": len(facts.by_class(IncomeClass.UNCLASSIFIED)),
            "by_income_type": by_type,
            "unclassified_types": sorted({row.income_type for row in facts.unclassified_rows}),
        },
        "daily_net_realized": facts.trading_net_realized,
        "assets": sorted({row.asset for row in facts.rows}),
        "elapsed_ms": elapsed_ms,
    }


@unittest.skipUnless(os.environ.get(LIVE_FLAG) == "1", f"set {LIVE_FLAG}=1 to run the authenticated live income read")
class BinanceIncomeLiveTest(unittest.TestCase):
    def test_sc16_real_income_history_is_read_completely(self) -> None:
        client = build_income_client()
        cutoff_ts = int(time.time() * 1000)
        day_start_ts = utc_day_start_ms(cutoff_ts)
        started = int(time.time() * 1000)

        facts = fetch_income_history(
            client,
            window_start_ms=day_start_ts,
            cutoff_ms=cutoff_ts,
            page_limit=int(os.environ.get(PAGE_LIMIT_ENV, str(MAX_PAGE_LIMIT))),
            max_pages=max_pages_from_env(),
        )
        elapsed_ms = int(time.time() * 1000) - started
        report = income_report(
            facts=facts, day_start_ts=day_start_ts, cutoff_ts=cutoff_ts, elapsed_ms=elapsed_ms
        )
        print("\nPROBEX INCOME HISTORY LIVE REPORT:\n" + json.dumps(report, indent=2, ensure_ascii=False))

        self.assertTrue(facts.coverage.complete, report)
        self.assertEqual(facts.unclassified_rows, (), report)  # 未识别类型必须暴露（不能静默）
        for row in facts.trading_rows:
            self.assertEqual(row.asset, "USDT", f"unsupported asset in trading row: {row}")
        self.assertEqual(facts.coverage.rows, len(facts.rows))

    def test_page_limit_is_bounded(self) -> None:
        with self.assertRaises(Exception):
            fetch_income_history(
                build_income_client(),
                window_start_ms=0,
                cutoff_ms=1,
                page_limit=MAX_PAGE_LIMIT + 1,
                max_pages=1,
            )


if __name__ == "__main__":
    unittest.main()
