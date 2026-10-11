"""P0017.1 Batch 1：Universe 发现（exchangeInfo → 可订阅永续集合）。"""

from __future__ import annotations

import unittest

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.universe import (
    DEFAULT_QUOTE_ASSETS,
    TRADING_STATUS,
    parse_universe,
)


def _sym(symbol, *, status=TRADING_STATUS, contract="PERPETUAL", quote="USDT", base=None,
         margin="USDT"):
    return {
        "symbol": symbol, "status": status, "contractType": contract,
        "baseAsset": base or symbol.replace(quote, ""), "quoteAsset": quote,
        "marginAsset": margin,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.1", "minPrice": "0.1", "maxPrice": "1e9"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1e6"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }


def _exchange_info(entries):
    return {"timezone": "UTC", "symbols": entries}


class UniverseDiscoveryTest(unittest.TestCase):
    def test_discovers_trading_usdt_perpetuals(self) -> None:
        universe = parse_universe(_exchange_info([_sym("BTCUSDT"), _sym("ETHUSDT")]))
        self.assertEqual(universe.symbols, ("BTCUSDT", "ETHUSDT"))
        btc = universe.get("BTCUSDT")
        self.assertEqual(btc.settlement_asset, "USDT")
        self.assertEqual(btc.view()["product_type"], "PERPETUAL")

    def test_excludes_non_trading_non_perpetual_and_other_quotes(self) -> None:
        universe = parse_universe(_exchange_info([
            _sym("BTCUSDT"),
            _sym("HALTUSDT", status="BREAK"),                # 非 TRADING
            _sym("BTCUSDC", quote="USDC", base="BTC"),        # quote 不允许
            _sym("Q1_2025", contract="CURRENT_QUARTER", quote="USDT"),  # 交割合约
        ]))
        self.assertEqual(universe.symbols, ("BTCUSDT",))
        reasons = dict(universe.skipped)
        self.assertIn("status=BREAK", reasons["HALTUSDT"])
        self.assertIn("quoteAsset=USDC", reasons["BTCUSDC"])
        self.assertIn("not a supported perpetual", reasons["Q1_2025"])

    def test_malformed_entry_is_skipped_not_fatal(self) -> None:
        universe = parse_universe(_exchange_info([_sym("BTCUSDT"), {"symbol": "BAD"}]))
        self.assertEqual(universe.symbols, ("BTCUSDT",))
        bad = dict(universe.skipped)
        self.assertIn("BAD", bad)
        self.assertIn("malformed", bad["BAD"])

    def test_duplicate_symbol_is_skipped(self) -> None:
        universe = parse_universe(_exchange_info([_sym("BTCUSDT"), _sym("BTCUSDT")]))
        self.assertEqual(universe.symbols, ("BTCUSDT",))
        self.assertTrue(any("duplicate" in r for _, r in universe.skipped))

    def test_top_level_structure_errors_are_fatal(self) -> None:
        with self.assertRaises(MarketDataFormatError):
            parse_universe({"symbols": "not-an-array"})
        with self.assertRaises(MarketDataFormatError):
            parse_universe({})

    def test_custom_quote_allowlist(self) -> None:
        universe = parse_universe(_exchange_info([_sym("BTCUSDT"), _sym("BTCUSDC", base="BTC",
                                                                          quote="USDC")]),
                                   quote_assets=frozenset({"USDT", "USDC"}))
        self.assertIn("BTCUSDT", universe.symbols)
        self.assertIn("BTCUSDC", universe.symbols)
        self.assertEqual(set(universe.quote_assets), {"USDC", "USDT"})

    def test_default_filter_is_usdt_only_and_non_empty(self) -> None:
        self.assertEqual(DEFAULT_QUOTE_ASSETS, frozenset({"USDT"}))
        universe = parse_universe(_exchange_info([]))
        self.assertEqual(universe.discovered, ())


if __name__ == "__main__":
    unittest.main()
