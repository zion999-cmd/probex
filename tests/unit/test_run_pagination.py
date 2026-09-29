"""F-19 `/api/v1/runs` 有界化：registry.page 的边界与游标语义。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from product.types import Fact, RuntimeIdentity, RuntimeMode
from storage.run_registry import (DEFAULT_RUN_PAGE_LIMIT, MAX_RUN_PAGE_LIMIT, JsonRunRegistry,
                                  RunRegistryError)


def identity(runtime_id: str) -> RuntimeIdentity:
    return RuntimeIdentity(mode=RuntimeMode.PAPER, environment="local", venue="binance",
                           symbol="BTCUSDT", runtime_id=runtime_id, started_at=1_000,
                           data_timestamp=Fact.unknown("no data"))


class PaginationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="probex-page-"))
        self.registry = JsonRunRegistry(self.tmp)
        for index in range(5):
            self.registry.start(runtime=identity(f"rt-{index}"), run_id=f"run-{index}",
                                now_ms=1_000 + index)
            self.registry.finalize(run_id=f"run-{index}", ended_at=2_000 + index, summary={})

    def test_default_limit_is_bounded(self) -> None:
        self.assertLessEqual(DEFAULT_RUN_PAGE_LIMIT, MAX_RUN_PAGE_LIMIT)
        self.assertGreater(DEFAULT_RUN_PAGE_LIMIT, 0)

    def test_page_returns_metadata(self) -> None:
        page = self.registry.page(limit=2, offset=0)
        self.assertEqual(len(page.items), 2)
        self.assertEqual(page.total, 5)
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_offset, 2)
        self.assertEqual(page.to_payload()["count"], 2)

    def test_offsets_cover_everything_without_duplicates(self) -> None:
        seen: list[str] = []
        offset = 0
        while True:
            page = self.registry.page(limit=2, offset=offset)
            seen.extend(record.run_id for record in page.items)
            if not page.has_more:
                break
            offset = page.next_offset or 0
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {f"run-{index}" for index in range(5)})

    def test_last_page_has_no_next_offset(self) -> None:
        page = self.registry.page(limit=2, offset=4)
        self.assertEqual(len(page.items), 1)
        self.assertFalse(page.has_more)
        self.assertIsNone(page.next_offset)

    def test_offset_past_the_end_is_empty_not_an_error(self) -> None:
        page = self.registry.page(limit=2, offset=99)
        self.assertEqual(page.items, ())
        self.assertFalse(page.has_more)

    def test_limit_is_required_bounded_and_positive(self) -> None:
        for bad in (0, -1, MAX_RUN_PAGE_LIMIT + 1):
            with self.subTest(limit=bad):
                with self.assertRaises(RunRegistryError):
                    self.registry.page(limit=bad)
        with self.assertRaises(RunRegistryError):
            self.registry.page(limit=1, offset=-1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
