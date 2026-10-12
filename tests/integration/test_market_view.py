"""Market View 只读端点测试（P0001.12.1 Market 工作区：Market View Symbol ≠ Runtime Trading Symbol）。

不触网：注入脚本化 JSON 客户端，验证

- 真实映射（K线/盘口/逐笔/标记价/24h 概览）与来源/时间戳；
- **按组件可用性**：单项失败只标该项（含 code），不伪装成“无数据”，全部失败才 503；
- 明确失败：格式非法 400、交易所不认 404、限流 502/503 语义；
- 目录搜索（选择器用）；
- **不修改运行实例交易标的**（服务只读、无写能力；UI 侧另有守卫）。
"""

from __future__ import annotations

import unittest

from connectors.binance.market_data.market_view import MarketViewError, MarketViewService

KLINES = [
    [1700000000000, "100.0", "101.0", "99.5", "100.5", "12.0", 1700000059999],
    [1700000060000, "100.5", "102.0", "100.4", "101.8", "9.0", 1700000119999],
]
DEPTH = {"lastUpdateId": 77, "E": 1700000060123,
         "bids": [["100.4", "3.0"], ["100.3", "5.0"]],
         "asks": [["100.6", "2.0"], ["100.7", "4.0"]]}
TRADES = [{"id": 1, "price": "100.55", "qty": "1.5", "quoteQty": "150.8",
           "time": 1700000060500, "isBuyerMaker": False},
          {"id": 2, "price": "100.45", "qty": "2.0", "quoteQty": "200.9",
           "time": 1700000060600, "isBuyerMaker": True}]
MARK = {"symbol": "BNBUSDT", "markPrice": "100.52", "indexPrice": "100.50",
        "lastFundingRate": "0.0001", "time": 1700000060700}
TICKER = {"symbol": "BNBUSDT", "lastPrice": "100.55", "priceChangePercent": "1.23",
          "highPrice": "102.0", "lowPrice": "99.0", "volume": "12345.0",
          "quoteVolume": "1234567.0"}
EXCHANGE_INFO = {"symbols": [
    {"symbol": "BNBUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
    {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
    {"symbol": "DEADUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "BREAK"},
]}


class FakeClient:
    """脚本化 JSON 客户端：按路径返回预设负载，或抛指定异常。"""

    def __init__(self, *, responses=None, failures=None) -> None:
        self.responses = responses or {}
        self.failures = failures or {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get_json(self, path, params, *, timeout_s):
        self.calls.append((path, dict(params)))
        if path in self.failures:
            raise self.failures[path]
        if path not in self.responses:
            raise AssertionError(f"unexpected path {path}")
        return self.responses[path]


def _service(**kwargs) -> MarketViewService:
    responses = {
        "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
        "/fapi/v1/klines": KLINES,
        "/fapi/v1/depth": DEPTH,
        "/fapi/v1/trades": TRADES,
        "/fapi/v1/premiumIndex": MARK,
        "/fapi/v1/ticker/24hr": TICKER,
    }
    client = kwargs.pop("client", None) or FakeClient(responses=responses)
    sleeps: list[float] = []
    service = MarketViewService(client=client, base_url="https://example.invalid",
                                clock=lambda: 1_700_000_100_000,
                                sleeper=sleeps.append, min_request_interval_ms=0,
                                rate_limit_retry_ms=0, **kwargs)
    service.test_sleeps = sleeps          # 供节流断言（测试资产）
    return service


class MarketViewServiceTest(unittest.TestCase):
    def test_view_maps_real_payloads_with_source_and_timestamp(self) -> None:
        service = _service()
        view = service.view("BNBUSDT", interval="1m", candles=2, depth_levels=5, trades=5)
        self.assertEqual(view["symbol"], "BNBUSDT")
        self.assertEqual(view["source"], "binance_public_rest")
        self.assertEqual(view["fetched_at_ms"], 1_700_000_100_000)
        self.assertEqual(view["view_kind"], "market_view")
        self.assertEqual(len(view["candles"]), 2)
        self.assertEqual(view["candles"][-1]["close"], 101.8)
        self.assertEqual(view["depth"]["best_bid"], 100.4)
        self.assertEqual(view["depth"]["best_ask"], 100.6)
        self.assertAlmostEqual(view["depth"]["spread"], 0.2, places=9)
        self.assertEqual(view["mark"]["price"], 100.52)
        self.assertEqual(view["overview"]["last_price"], 100.55)
        self.assertEqual(view["trades"][0]["aggressor"], "buy")
        self.assertEqual(view["trades"][1]["aggressor"], "sell")
        self.assertEqual(view["available"],
                         {"candles": True, "depth": True, "trades": True, "mark": True,
                          "overview": True})

    def test_symbols_view_searches_catalog(self) -> None:
        service = _service()
        payload = service.symbols_view(query="bnb")
        self.assertEqual([row["symbol"] for row in payload["symbols"]], ["BNBUSDT"])
        # BREAK 状态不进入可查看集合
        all_symbols = service.symbols_view()
        self.assertEqual([row["symbol"] for row in all_symbols["symbols"]],
                         ["BNBUSDT", "ETHUSDT"])

    def test_invalid_symbol_format_is_rejected_without_network(self) -> None:
        client = FakeClient(responses={})
        service = _service(client=client)
        with self.assertRaises(MarketViewError) as ctx:
            service.view("not a symbol!")
        self.assertEqual(ctx.exception.code, "invalid_symbol")
        self.assertEqual(client.calls, [])

    def test_unsupported_symbol_is_explicit(self) -> None:
        service = _service()
        with self.assertRaises(MarketViewError) as ctx:
            service.view("DEADUSDT")
        self.assertEqual(ctx.exception.code, "unsupported_symbol")

    def test_unknown_symbol_from_catalog_is_unsupported(self) -> None:
        service = _service()
        with self.assertRaises(MarketViewError) as ctx:
            service.view("NOPEUSDT")
        self.assertEqual(ctx.exception.code, "unsupported_symbol")

    def test_partial_availability_reports_reason_per_component(self) -> None:
        """逐笔/标记价失败 ⇒ 只标该两项，其余仍返回真实数据（不伪装成 0/无数据）。"""
        responses = {
            "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
            "/fapi/v1/klines": KLINES,
            "/fapi/v1/depth": DEPTH,
            "/fapi/v1/ticker/24hr": TICKER,
        }
        client = FakeClient(responses=responses,
                            failures={"/fapi/v1/trades": RuntimeError("HTTP 418 for url"),
                                      "/fapi/v1/premiumIndex": RuntimeError("boom")})
        service = _service(client=client)
        view = service.view("BNBUSDT")
        self.assertTrue(view["available"]["candles"])
        self.assertTrue(view["available"]["depth"])
        self.assertTrue(view["available"]["overview"])
        self.assertFalse(view["available"]["trades"])
        self.assertFalse(view["available"]["mark"])
        self.assertEqual(view["errors"]["trades"]["code"], "rate_limited")
        self.assertTrue(view["errors"]["mark"]["code"])
        self.assertEqual(view["trades"], [])
        self.assertIsNone(view["mark"])

    def test_all_components_failing_is_a_clear_service_error(self) -> None:
        client = FakeClient(responses={"/fapi/v1/exchangeInfo": EXCHANGE_INFO},
                            failures={"/fapi/v1/klines": RuntimeError("HTTP 418"),
                                      "/fapi/v1/depth": RuntimeError("HTTP 418"),
                                      "/fapi/v1/trades": RuntimeError("HTTP 418"),
                                      "/fapi/v1/premiumIndex": RuntimeError("HTTP 418"),
                                      "/fapi/v1/ticker/24hr": RuntimeError("HTTP 418")})
        service = _service(client=client)
        with self.assertRaises(MarketViewError) as ctx:
            service.view("BNBUSDT")
        self.assertEqual(ctx.exception.code, "market_view_unavailable")
        self.assertIn("rate_limited", ctx.exception.detail)

    def test_venue_400_means_unsupported_symbol(self) -> None:
        client = FakeClient(responses={"/fapi/v1/exchangeInfo": EXCHANGE_INFO},
                            failures={"/fapi/v1/klines": RuntimeError("HTTP 400 for url")})
        service = _service(client=client)
        with self.assertRaises(MarketViewError) as ctx:
            service.view("BNBUSDT")
        self.assertEqual(ctx.exception.code, "unsupported_symbol")

    def test_catalog_unavailable_still_allows_viewing(self) -> None:
        """目录不可用不应阻止查看行情：用交易所响应作为权威，并记录 catalog 事实。"""
        responses = {"/fapi/v1/klines": KLINES, "/fapi/v1/depth": DEPTH}
        client = FakeClient(responses=responses,
                            failures={"/fapi/v1/exchangeInfo": RuntimeError("HTTP 418")})
        service = _service(client=client)
        view = service.view("BNBUSDT", candles=1)
        self.assertTrue(view["available"]["candles"])
        self.assertTrue(any("catalog" in note for note in view["notes"]))

    def test_interval_is_validated(self) -> None:
        service = _service()
        with self.assertRaises(MarketViewError) as ctx:
            service.view("BNBUSDT", interval="7m")
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_short_ttl_cache_avoids_burst(self) -> None:
        """极短 TTL 缓存：连拍同一请求只打一次上游（防交易所限流）。"""
        responses = {"/fapi/v1/exchangeInfo": EXCHANGE_INFO, "/fapi/v1/klines": KLINES,
                     "/fapi/v1/depth": DEPTH, "/fapi/v1/trades": TRADES,
                     "/fapi/v1/premiumIndex": MARK, "/fapi/v1/ticker/24hr": TICKER}
        client = FakeClient(responses=responses)
        service = _service(client=client, cache_ttl_ms=60_000)
        service.view("BNBUSDT")
        first = len(client.calls)
        service.view("BNBUSDT")
        self.assertEqual(len(client.calls), first)

    def test_components_parameter_limits_upstream_calls(self) -> None:
        """`components=depth,mark` 只打这两个上游端点（助手/局部视图用，减少限流风险）。"""
        responses = {"/fapi/v1/exchangeInfo": EXCHANGE_INFO, "/fapi/v1/depth": DEPTH,
                     "/fapi/v1/premiumIndex": MARK}
        client = FakeClient(responses=responses)
        service = _service(client=client)
        view = service.view("BNBUSDT", components=("depth", "mark"))
        self.assertEqual({path for path, _ in client.calls},
                         {"/fapi/v1/exchangeInfo", "/fapi/v1/depth", "/fapi/v1/premiumIndex"})
        self.assertEqual(view["available"], {"depth": True, "mark": True})
        self.assertEqual(view["candles"], [])
        self.assertIsNone(view["overview"])

    def test_unknown_component_is_rejected(self) -> None:
        service = _service()
        with self.assertRaises(MarketViewError) as ctx:
            service.view("BNBUSDT", components=("depth", "nope"))
        self.assertEqual(ctx.exception.code, "invalid_components")

    def test_limits_are_bounded(self) -> None:
        service = _service()
        service.view("BNBUSDT", candles=99_999, depth_levels=99_999, trades=99_999)
        klines_params = [p for path, p in service._client.calls if path == "/fapi/v1/klines"][0]
        self.assertEqual(klines_params["limit"], "1000")
        depth_params = [p for path, p in service._client.calls if path == "/fapi/v1/depth"][0]
        self.assertEqual(depth_params["limit"], "500")


if __name__ == "__main__":
    unittest.main()
