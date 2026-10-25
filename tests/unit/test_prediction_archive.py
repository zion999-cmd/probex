"""D-055 批次1：PredictionRecord 的 durable JSONL archive。

跨进程复现语义：
- append-only JSONL（temp→fsync→rename 由 append + fsync 保证；每行内容寻址 sha256）；
- 重启加载严格校验（损坏行 / hash 不符 / 未知字段版本 / 枚举非法 ⇒ fail-closed）；
- 同一 `(market_state_hash, question_schema_version)` 多条记录时 `find` 返回文件顺序最早一条，
  与 `InMemoryPredictionArchive` 语义一致；
- archive 不调用 provider、不做预测（纯证据存储）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from prediction.types import (
    FutureReturnDistribution,
    Prediction,
    PredictionMode,
    PredictionRecord,
    ProviderUsage,
)
from storage.prediction_archive import (
    JsonlPredictionArchive,
    PredictionArchiveError,
)

HORIZONS = (5_000, 15_000, 30_000, 60_000)


def _distribution(horizon_ms: int, strong: float) -> FutureReturnDistribution:
    rest = (1.0 - strong) / 4.0
    return FutureReturnDistribution(
        horizon_ms=horizon_ms, strong_down=strong, down=rest, flat=rest,
        up=rest, strong_up=rest)


def _prediction() -> Prediction:
    return Prediction(
        future_return=tuple(_distribution(h, 0.1 + i * 0.01)
                              for i, h in enumerate(HORIZONS)),
        buy_adverse_selection=0.2, sell_adverse_selection=0.3,
        buy_fill_probability=0.4, sell_fill_probability=0.5,
        provider_confidence=0.6, derived_confidence=0.7)


def _record(seq: int = 1, *, state_hash: str = "hash-1",
            mode: PredictionMode = PredictionMode.LIVE_REQUERY,
            response_id: str | None = "resp-1") -> PredictionRecord:
    return PredictionRecord(
        request_id=f"req-{seq}", sequence=seq, market_state_hash=state_hash,
        feature_schema_version="market-state-v1",
        question_schema_version="jev-market-v1",
        provider="systemone", model="jev-1.13", mode=mode,
        as_of=10_000 + seq, request_created_at=10_000 + seq,
        response_received_at=10_400 + seq, latency_ms=400,
        expires_at=20_000 + seq, raw_response='{"answers":{}}',
        prediction=_prediction(), requested_model="jev-1.13",
        resolved_model="typesafe/jev-1.13-20260917", response_id=response_id,
        usage=ProviderUsage(input_tokens=100, output_tokens=20, cost=0.0001))


class JsonlPredictionArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="pred-archive-"))
        self.path = self.tmp / "predictions.jsonl"

    def test_store_and_find_in_process(self) -> None:
        archive = JsonlPredictionArchive(self.path)
        archive.store(_record())
        found = archive.find(market_state_hash="hash-1",
                              question_schema_version="jev-market-v1")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.request_id, "req-1")
        self.assertEqual(found.prediction.future_return[0].horizon_ms, 5_000)
        self.assertAlmostEqual(found.prediction.future_return[0].strong_down, 0.1)
        self.assertEqual(found.usage.input_tokens, 100)  # type: ignore[union-attr]
        self.assertEqual(found.mode, PredictionMode.LIVE_REQUERY)

    def test_record_survives_restart_with_full_evidence(self) -> None:
        JsonlPredictionArchive(self.path).store(_record())
        reopened = JsonlPredictionArchive(self.path)
        found = reopened.find(market_state_hash="hash-1",
                                question_schema_version="jev-market-v1")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.request_id, "req-1")
        self.assertEqual(found.resolved_model, "typesafe/jev-1.13-20260917")
        self.assertEqual(found.raw_response, '{"answers":{}}')
        self.assertEqual(found.prediction.provider_confidence, 0.6)
        self.assertEqual(found.usage.cost, 0.0001)  # type: ignore[union-attr]

    def test_find_returns_earliest_record_for_same_state(self) -> None:
        archive = JsonlPredictionArchive(self.path)
        archive.store(_record(1))
        archive.store(_record(2))
        self.assertEqual(
            archive.find(market_state_hash="hash-1",
                          question_schema_version="jev-market-v1").request_id,  # type: ignore[union-attr]
            "req-1")
        # 跨进程保持"最早一条"
        self.assertEqual(
            JsonlPredictionArchive(self.path).find(
                market_state_hash="hash-1", question_schema_version="jev-market-v1").request_id,  # type: ignore[union-attr]
            "req-1")

    def test_find_filters_by_question_schema_version(self) -> None:
        record = _record()
        archive = JsonlPredictionArchive(self.path)
        archive.store(record)
        self.assertIsNone(archive.find(market_state_hash="hash-1",
                                          question_schema_version="jev-market-v9"))
        self.assertIsNone(archive.find(market_state_hash="other",
                                          question_schema_version="jev-market-v1"))

    def test_unknown_key_returns_none_and_missing_file_is_empty(self) -> None:
        archive = JsonlPredictionArchive(self.path)
        self.assertIsNone(archive.find(market_state_hash="nope",
                                          question_schema_version="jev-market-v1"))
        self.assertEqual(len(archive), 0)

    def test_recorded_mode_roundtrip(self) -> None:
        archive = JsonlPredictionArchive(self.path)
        archive.store(_record(1, mode=PredictionMode.RECORDED))
        found = JsonlPredictionArchive(self.path).find(
            market_state_hash="hash-1", question_schema_version="jev-market-v1")
        self.assertEqual(found.mode, PredictionMode.RECORDED)  # type: ignore[union-attr]

    def test_optional_evidence_fields_tolerated_as_none(self) -> None:
        record = _record()
        # 旧记录可能没有 P0001.4.2 追加字段（默认 None），用对象替换默认值
        from dataclasses import replace
        minimal = replace(record, requested_model=None, resolved_model=None,
                            response_id=None, usage=None)
        archive = JsonlPredictionArchive(self.path)
        archive.store(minimal)
        found = JsonlPredictionArchive(self.path).find(
            market_state_hash="hash-1", question_schema_version="jev-market-v1")
        self.assertIsNone(found.usage)  # type: ignore[union-attr]
        self.assertIsNone(found.response_id)  # type: ignore[union-attr]

    def test_corrupt_line_fails_closed(self) -> None:
        JsonlPredictionArchive(self.path).store(_record())
        with self.path.open("a", encoding="utf-8") as h:
            h.write("{not json\n")
        with self.assertRaises(PredictionArchiveError):
            JsonlPredictionArchive(self.path)

    def test_tampered_content_fails_closed(self) -> None:
        JsonlPredictionArchive(self.path).store(_record())
        lines = self.path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("req-1", "req-9")
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(PredictionArchiveError):
            JsonlPredictionArchive(self.path)

    def test_unknown_archive_schema_fails_closed(self) -> None:
        JsonlPredictionArchive(self.path).store(_record())
        lines = self.path.read_text(encoding="utf-8").splitlines()
        import json
        row = json.loads(lines[0])
        row["archive_schema"] = "prediction-archive/v9"
        self.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(PredictionArchiveError):
            JsonlPredictionArchive(self.path)

    def test_len_counts_records(self) -> None:
        archive = JsonlPredictionArchive(self.path)
        archive.store(_record(1, state_hash="h1"))
        archive.store(_record(2, state_hash="h2"))
        self.assertEqual(len(archive), 2)
        self.assertEqual(len(JsonlPredictionArchive(self.path)), 2)


if __name__ == "__main__":
    unittest.main()
