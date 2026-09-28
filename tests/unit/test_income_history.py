"""P0001.9.4.1 单元测试：income 事实解析 / 分类 / 去重 / 分页 / 资产契约。"""

from __future__ import annotations

import unittest

from connectors.binance.private.income import (
    MAX_PAGE_LIMIT,
    IncomeClass,
    IncomeHistoryConflictError,
    IncomeHistoryError,
    IncomeHistoryFacts,
    UnsupportedIncomeAssetError,
    classify_income_type,
    dedupe_income_rows,
    fetch_income_history,
    parse_income_rows,
    verify_settlement_asset,
)
from connectors.binance.private.rest import PrivateRestClient
from tests.private_support import FakeRestFetcher, credentials
from tests.support import BASE_TS


def row(
    *,
    income_type: str = "REALIZED_PNL",
    income: str = "1.5",
    asset: str = "USDT",
    time_ms: int = BASE_TS,
    tran_id: int = 1,
    trade_id: object = "t1",
    symbol: object = "BTCUSDT",
    info: object = "",
) -> dict:
    """真实形态的 income 行（2026-09-28 实测键集合）。"""
    return {
        "symbol": symbol,
        "incomeType": income_type,
        "income": income,
        "asset": asset,
        "time": time_ms,
        "info": info,
        "tradeId": trade_id,
        "tranId": tran_id,
    }


class ParseTest(unittest.TestCase):
    def test_real_shape_is_parsed(self) -> None:
        rows = parse_income_rows([row()])

        self.assertEqual(len(rows), 1)
        parsed = rows[0]
        self.assertEqual(parsed.income_type, "REALIZED_PNL")
        self.assertEqual(parsed.income, 1.5)
        self.assertEqual(parsed.asset, "USDT")
        self.assertEqual(parsed.time_ms, BASE_TS)
        self.assertEqual(parsed.tran_id, 1)
        self.assertEqual(parsed.trade_id, "t1")
        self.assertEqual(parsed.info, None)

    def test_empty_strings_mean_absent(self) -> None:
        """真实 TRANSFER 行的 symbol/tradeId/info 都是空串（不是缺字段）。"""
        parsed = parse_income_rows([row(income_type="TRANSFER", trade_id="", symbol="", info="TRANSFER")])[0]

        self.assertIsNone(parsed.trade_id)
        self.assertIsNone(parsed.symbol)
        self.assertEqual(parsed.info, "TRANSFER")

    def test_missing_or_wrong_fields_fail_closed(self) -> None:
        """报文结构错误 ⇒ 私有层既有契约 `PrivateFormatError`（仍是私有错误域，不外泄）。"""
        from connectors.binance.private.errors import PrivateApiError, PrivateFormatError

        for field in ("incomeType", "income", "asset", "time", "tranId"):
            raw = row()
            del raw[field]
            with self.subTest(field=field):
                with self.assertRaises(PrivateFormatError) as ctx:
                    parse_income_rows([raw])
                self.assertIsInstance(ctx.exception, PrivateApiError)
        for bad in ({"trade_id": 5}, {"symbol": 5}, {"info": 5}):
            with self.subTest(bad=bad):
                with self.assertRaises(PrivateFormatError):
                    parse_income_rows([row(**bad)])

    def test_response_must_be_an_array(self) -> None:
        from connectors.binance.private.errors import PrivateFormatError

        with self.assertRaises(PrivateFormatError):
            parse_income_rows({"incomeType": "REALIZED_PNL"})

    def test_income_specific_errors_survive_translation(self) -> None:
        """P0001.9.4.1：income 语义错误必须保留自己的类型（用于区分 BLOCKED 原因）。"""
        with self.assertRaises(IncomeHistoryConflictError):
            dedupe_income_rows(parse_income_rows([row(tran_id=1, income="1"), row(tran_id=1, income="2")]))
        with self.assertRaises(UnsupportedIncomeAssetError):
            verify_settlement_asset(parse_income_rows([row(asset="BTC")]))


class ClassificationTest(unittest.TestCase):
    def test_sc5_trading_types(self) -> None:
        for income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "SPECIAL_FUNDING_FEE"):
            with self.subTest(income_type=income_type):
                self.assertIs(classify_income_type(income_type), IncomeClass.TRADING)

    def test_sc6_transfer_is_not_trading(self) -> None:
        self.assertIs(classify_income_type("TRANSFER"), IncomeClass.NON_TRADING)

    def test_sc7_unknown_type_is_unclassified(self) -> None:
        for income_type in ("INSURANCE_CLEAR", "REFERRAL_KICKBACK", "SOMETHING_NEW_2027", ""):
            with self.subTest(income_type=income_type):
                self.assertIs(classify_income_type(income_type), IncomeClass.UNCLASSIFIED)

    def test_trading_sum_keeps_exchange_signs(self) -> None:
        """SC-5：Binance 的 income 已带符号 ⇒ 直接相加（不再人工取负）。"""
        rows = parse_income_rows(
            [
                row(income_type="REALIZED_PNL", income="0.79", tran_id=1),
                row(income_type="COMMISSION", income="-0.02", tran_id=2),
                row(income_type="FUNDING_FEE", income="-0.01", tran_id=3),
                row(income_type="SPECIAL_FUNDING_FEE", income="-0.005", tran_id=4),
                row(income_type="TRANSFER", income="5000.0", tran_id=0),
            ]
        )
        facts = IncomeHistoryFacts(
            rows=rows, coverage=_coverage(rows), duplicates_dropped=0
        )

        self.assertAlmostEqual(facts.trading_net_realized, 0.755)
        self.assertEqual(len(facts.non_trading_rows), 1)


class AuditIdentityRulingTest(unittest.TestCase):
    """人类裁决 2026-09-28：审计身份不得污染风险计算（P0001.9.4.1 correction）。"""

    def test_two_different_transfers_with_same_tran_id_coexist(self) -> None:
        rows = parse_income_rows(
            [
                row(income_type="TRANSFER", income="5000.0", time_ms=BASE_TS, tran_id=0),
                row(income_type="TRANSFER", income="250.0", time_ms=BASE_TS + 1, tran_id=0),
            ]
        )

        deduped, dropped = dedupe_income_rows(rows)

        self.assertEqual(len(deduped), 2)  # tranId=0 不当作业务身份
        self.assertEqual(dropped, 0)
        facts = IncomeHistoryFacts(rows=deduped, coverage=_coverage(deduped), duplicates_dropped=dropped)
        self.assertEqual(facts.trading_net_realized, 0.0)  # 不影响 daily PnL
        self.assertEqual(len(facts.non_trading_rows), 2)

    def test_identical_transfer_rows_are_audit_deduped(self) -> None:
        rows = parse_income_rows(
            [
                row(income_type="TRANSFER", income="5000.0", time_ms=BASE_TS, tran_id=0),
                row(income_type="TRANSFER", income="5000.0", time_ms=BASE_TS, tran_id=0),
            ]
        )

        deduped, dropped = dedupe_income_rows(rows)

        self.assertEqual(len(deduped), 1)
        self.assertEqual(dropped, 1)
        self.assertEqual(IncomeHistoryFacts(rows=deduped, coverage=_coverage(deduped), duplicates_dropped=dropped).trading_net_realized, 0.0)

    def test_unclassified_rows_with_same_key_do_not_conflict(self) -> None:
        """未分类行同样属于审计层（风险结果已经是 BLOCKED，不应改报 HISTORY_CONFLICT）。"""
        rows = parse_income_rows(
            [
                row(income_type="FEE_RETURN", income="0.1", time_ms=BASE_TS, tran_id=0),
                row(income_type="FEE_RETURN", income="0.2", time_ms=BASE_TS + 1, tran_id=0),
            ]
        )

        deduped, _dropped = dedupe_income_rows(rows)

        self.assertEqual(len(deduped), 2)
        self.assertEqual(len(IncomeHistoryFacts(rows=deduped, coverage=_coverage(deduped), duplicates_dropped=0).unclassified_rows), 2)

    def test_trading_conflict_is_still_strict(self) -> None:
        rows = parse_income_rows(
            [
                row(income_type="REALIZED_PNL", income="1.0", tran_id=7),
                row(income_type="REALIZED_PNL", income="2.0", tran_id=7),
            ]
        )

        with self.assertRaises(IncomeHistoryConflictError):
            dedupe_income_rows(rows)

    def test_whitelist_is_not_extended(self) -> None:
        """裁决 2：只有提案验证过的 4 类计入 PnL；非交易类只认 TRANSFER。"""
        from connectors.binance.private.income import NON_TRADING_INCOME_TYPES, TRADING_INCOME_TYPES

        self.assertEqual(
            TRADING_INCOME_TYPES, {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "SPECIAL_FUNDING_FEE"}
        )
        self.assertEqual(NON_TRADING_INCOME_TYPES, {"TRANSFER"})
        for income_type in ("FEE_RETURN", "INSURANCE_CLEAR", "WELCOME_BONUS", "REFERRAL_KICKBACK", "DELIVERED_SETTLE"):
            with self.subTest(income_type=income_type):
                self.assertIs(classify_income_type(income_type), IncomeClass.UNCLASSIFIED)


class DedupeTest(unittest.TestCase):
    def test_sc3_identical_duplicates_are_counted_once(self) -> None:
        rows = parse_income_rows([row(tran_id=7), row(tran_id=7), row(tran_id=8)])

        deduped, dropped = dedupe_income_rows(rows)

        self.assertEqual(len(deduped), 2)
        self.assertEqual(dropped, 1)

    def test_sc4_same_key_different_content_conflicts(self) -> None:
        rows = parse_income_rows([row(tran_id=7, income="1.0"), row(tran_id=7, income="2.0")])

        with self.assertRaises(IncomeHistoryConflictError) as ctx:
            dedupe_income_rows(rows)

        self.assertIn("HISTORY_CONFLICT", str(ctx.exception))

    def test_dedupe_is_idempotent(self) -> None:
        rows = parse_income_rows([row(tran_id=1), row(tran_id=2)])

        once, _ = dedupe_income_rows(rows)
        twice, dropped = dedupe_income_rows(list(once) * 3)

        self.assertEqual(len(twice), 2)
        self.assertEqual(dropped, 4)


class AssetContractTest(unittest.TestCase):
    def test_sc8_trading_row_in_other_asset_blocks(self) -> None:
        rows = parse_income_rows([row(asset="BTC")])

        with self.assertRaises(UnsupportedIncomeAssetError) as ctx:
            verify_settlement_asset(rows)

        self.assertIn("UNSUPPORTED_INCOME_ASSET", str(ctx.exception))

    def test_non_trading_row_in_other_asset_is_allowed(self) -> None:
        rows = parse_income_rows([row(income_type="TRANSFER", asset="BTC")])

        verify_settlement_asset(rows)  # 不抛异常：非交易资金流只记录、不参与 PnL


def _coverage(rows, *, complete: bool = True, pages: int = 1, reason: str = ""):
    from connectors.binance.private.income import IncomeHistoryCoverage

    times = [entry.time_ms for entry in rows]
    return IncomeHistoryCoverage(
        start_ts=min(times) if times else BASE_TS,
        end_ts=max(times) if times else BASE_TS,
        rows=len(rows),
        pages=pages,
        complete=complete,
        reason=reason,
    )


class PaginationTest(unittest.TestCase):
    def _client(self, pages: list[list[dict]]) -> PrivateRestClient:
        responses: dict[str, object] = {}
        fetcher = FakeRestFetcher(responses=responses)
        client = PrivateRestClient(credentials=credentials(), fetcher=fetcher)
        for index, page_rows in enumerate(pages, start=1):
            fetcher.sequences.setdefault("/fapi/v1/income", []).append(page_rows)
        return client

    def test_sc2_multi_page_history_is_read_completely(self) -> None:
        page_one = [row(tran_id=index, time_ms=BASE_TS + index) for index in range(3)]
        page_two = [row(tran_id=100 + index, time_ms=BASE_TS + 100 + index) for index in range(2)]
        client = self._client([page_one, page_two])

        facts = fetch_income_history(
            client, window_start_ms=BASE_TS, cutoff_ms=BASE_TS + 1_000, page_limit=3, max_pages=5
        )

        self.assertTrue(facts.coverage.complete)
        self.assertEqual(facts.coverage.pages, 2)
        self.assertEqual(facts.coverage.rows, 5)

    def test_sc12_running_out_of_pages_is_incomplete(self) -> None:
        full_page = [row(tran_id=index, time_ms=BASE_TS + index) for index in range(3)]
        client = self._client([full_page, full_page])

        facts = fetch_income_history(
            client, window_start_ms=BASE_TS, cutoff_ms=BASE_TS + 1_000, page_limit=3, max_pages=2
        )

        self.assertFalse(facts.coverage.complete)
        self.assertIn("max_pages", facts.coverage.reason)

    def test_empty_window_is_complete_and_zero_rows(self) -> None:
        """SC-11：完整零流水窗口 = 已知的 0 行（不是 UNKNOWN）。"""
        client = self._client([[]])

        facts = fetch_income_history(
            client, window_start_ms=BASE_TS, cutoff_ms=BASE_TS + 10, page_limit=MAX_PAGE_LIMIT, max_pages=3
        )

        self.assertTrue(facts.coverage.complete)
        self.assertEqual(facts.coverage.rows, 0)
        self.assertEqual(facts.trading_net_realized, 0.0)

    def test_rows_after_cutoff_make_coverage_incomplete(self) -> None:
        client = self._client([[row(time_ms=BASE_TS + 5_000)]])

        facts = fetch_income_history(
            client, window_start_ms=BASE_TS, cutoff_ms=BASE_TS + 10, page_limit=10, max_pages=3
        )

        self.assertFalse(facts.coverage.complete)
        self.assertIn("cutoff", facts.coverage.reason)

    def test_invalid_arguments_fail_closed(self) -> None:
        client = self._client([[]])
        for kwargs in (
            {"max_pages": 0},
            {"max_pages": True},
            {"page_limit": 0},
            {"page_limit": 1_001},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(IncomeHistoryError):
                    fetch_income_history(
                        client,
                        window_start_ms=BASE_TS,
                        cutoff_ms=BASE_TS + 1,
                        page_limit=kwargs.get("page_limit", 10),
                        max_pages=kwargs.get("max_pages", 3),
                    )

    def test_rest_rejects_out_of_range_windows(self) -> None:
        client = self._client([[]])
        with self.assertRaises(Exception):
            client.income_history(start_time=10, end_time=5, page=1, limit=10)
        with self.assertRaises(Exception):
            client.income_history(start_time=0, end_time=10, page=1, limit=0)


if __name__ == "__main__":
    unittest.main()
