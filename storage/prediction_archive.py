"""D-055 批次1：`PredictionRecord` 的 durable JSONL archive。

- append-only 文本行：`{archive_schema, record_sha256, record}`；
  `record_sha256 = sha256(canonical_json(record))`，加载时重算（篡改 fail-closed）。
- 严格加载：archive schema 版本受信、枚举合法、概率/时间等由 `PredictionRecord`
  自身不变量校验；任何损坏 ⇒ `PredictionArchiveError`，不静默跳过。
- 语义与 `InMemoryPredictionArchive` 一致：
  同 `(market_state_hash, question_schema_version)` 多条记录时 `find` 返回最早一条。
- 纯证据存储：不调用 provider、不联网、不使用 wall-clock；单 writer（与 Event Store 同纪律）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from prediction.types import (
    FutureReturnDistribution,
    Prediction,
    PredictionMode,
    PredictionRecord,
    ProviderUsage,
)

ARCHIVE_SCHEMA = "prediction-archive/v1"

_RECORD_FIELDS = (
    "request_id", "sequence", "market_state_hash", "feature_schema_version",
    "question_schema_version", "provider", "model", "as_of", "request_created_at",
    "response_received_at", "latency_ms", "expires_at", "raw_response",
    "requested_model", "resolved_model", "response_id",
)
_CATEGORIES = ("strong_down", "down", "flat", "up", "strong_up")


class PredictionArchiveError(Exception):
    """durable prediction archive 损坏或契约不符。"""


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_to_dict(record: PredictionRecord) -> dict[str, object]:
    prediction = record.prediction
    return {
        **{name: getattr(record, name) for name in _RECORD_FIELDS},
        "mode": record.mode.value,
        "prediction": {
            "future_return": [
                {"horizon_ms": d.horizon_ms,
                 **{c: getattr(d, c) for c in _CATEGORIES}}
                for d in prediction.future_return
            ],
            "buy_adverse_selection": prediction.buy_adverse_selection,
            "sell_adverse_selection": prediction.sell_adverse_selection,
            "buy_fill_probability": prediction.buy_fill_probability,
            "sell_fill_probability": prediction.sell_fill_probability,
            "provider_confidence": prediction.provider_confidence,
            "derived_confidence": prediction.derived_confidence,
        },
        "usage": (None if record.usage is None else {
            "input_tokens": record.usage.input_tokens,
            "output_tokens": record.usage.output_tokens,
            "cost": record.usage.cost,
        }),
    }


def _require(condition: bool, where: str, detail: str) -> None:
    if not condition:
        raise PredictionArchiveError(f"{where}: {detail}")


def _record_from_dict(row: object, *, where: str) -> PredictionRecord:
    _require(isinstance(row, dict), where, "record must be a JSON object")
    assert isinstance(row, dict)
    for name in _RECORD_FIELDS:
        _require(name in row, where, f"missing field {name!r}")
    try:
        mode = PredictionMode(str(row["mode"]))
    except ValueError:
        raise PredictionArchiveError(f"{where}: invalid mode {row.get('mode')!r}") from None

    pdict = row.get("prediction")
    _require(isinstance(pdict, dict), where, "prediction must be an object")
    assert isinstance(pdict, dict)
    dist_raw = pdict.get("future_return")
    _require(isinstance(dist_raw, list) and dist_raw, where,
             "prediction.future_return must be a non-empty list")
    distributions = []
    for index, item in enumerate(dist_raw):  # type: ignore[union-attr]
        pwhere = f"{where}.prediction.future_return[{index}]"
        _require(isinstance(item, dict), pwhere, "distribution must be an object")
        assert isinstance(item, dict)
        _require(isinstance(item.get("horizon_ms"), int), pwhere, "horizon_ms must be int")
        try:
            distributions.append(FutureReturnDistribution(
                horizon_ms=int(item["horizon_ms"]),
                **{c: float(item[c]) for c in _CATEGORIES}))
        except (KeyError, TypeError, ValueError) as exc:
            raise PredictionArchiveError(f"{pwhere}: invalid distribution: {exc}") from None
    try:
        prediction = Prediction(
            future_return=tuple(distributions),
            buy_adverse_selection=float(pdict["buy_adverse_selection"]),
            sell_adverse_selection=float(pdict["sell_adverse_selection"]),
            buy_fill_probability=float(pdict["buy_fill_probability"]),
            sell_fill_probability=float(pdict["sell_fill_probability"]),
            provider_confidence=(None if pdict.get("provider_confidence") is None
                                   else float(pdict["provider_confidence"])),
            derived_confidence=float(pdict["derived_confidence"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PredictionArchiveError(f"{where}.prediction: invalid fields: {exc}") from None

    usage_raw = row.get("usage")
    _require(usage_raw is None or isinstance(usage_raw, dict), where,
             "usage must be an object or null")
    usage = None
    if isinstance(usage_raw, dict):
        try:
            usage = ProviderUsage(
                input_tokens=(None if usage_raw.get("input_tokens") is None
                                else int(usage_raw["input_tokens"])),
                output_tokens=(None if usage_raw.get("output_tokens") is None
                                 else int(usage_raw["output_tokens"])),
                cost=(None if usage_raw.get("cost") is None
                        else float(usage_raw["cost"])))
        except (TypeError, ValueError) as exc:
            raise PredictionArchiveError(f"{where}.usage: invalid fields: {exc}") from None

    try:
        return PredictionRecord(
            request_id=str(row["request_id"]),
            sequence=int(row["sequence"]),
            market_state_hash=str(row["market_state_hash"]),
            feature_schema_version=str(row["feature_schema_version"]),
            question_schema_version=str(row["question_schema_version"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            mode=mode,
            as_of=int(row["as_of"]),
            request_created_at=int(row["request_created_at"]),
            response_received_at=int(row["response_received_at"]),
            latency_ms=int(row["latency_ms"]),
            expires_at=int(row["expires_at"]),
            raw_response=str(row["raw_response"]),
            prediction=prediction,
            requested_model=row.get("requested_model"),
            resolved_model=row.get("resolved_model"),
            response_id=row.get("response_id"),
            usage=usage)
    except (KeyError, TypeError, ValueError) as exc:
        raise PredictionArchiveError(f"{where}: invalid record fields: {exc}") from None


class JsonlPredictionArchive:
    """append-only JSONL 的 `PredictionArchive` 实现（跨进程可复现）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._records: list[PredictionRecord] = []
        self._index: dict[tuple[str, str], int] = {}
        if self._path.exists():
            self._load()

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PredictionArchiveError(f"{self._path}: cannot read archive: {exc}") from None
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            where = f"{self._path}:{number}"
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PredictionArchiveError(f"{where}: corrupt JSON: {exc}") from None
            if not isinstance(envelope, dict):
                raise PredictionArchiveError(f"{where}: envelope must be a JSON object")
            if envelope.get("archive_schema") != ARCHIVE_SCHEMA:
                raise PredictionArchiveError(
                    f"{where}: unsupported archive_schema {envelope.get('archive_schema')!r}")
            record_dict = envelope.get("record")
            digest = envelope.get("record_sha256")
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise PredictionArchiveError(f"{where}: record_sha256 missing/invalid")
            actual = "sha256:" + hashlib.sha256(
                _canonical(record_dict).encode("utf-8")).hexdigest()
            if actual != digest:
                raise PredictionArchiveError(f"{where}: record_sha256 mismatch")
            record = _record_from_dict(record_dict, where=where)
            key = (record.market_state_hash, record.question_schema_version)
            self._index.setdefault(key, len(self._records))
            self._records.append(record)

    def store(self, record: PredictionRecord) -> None:
        record_dict = _record_to_dict(record)
        digest = "sha256:" + hashlib.sha256(
            _canonical(record_dict).encode("utf-8")).hexdigest()
        envelope = {"archive_schema": ARCHIVE_SCHEMA,
                     "record_sha256": digest, "record": record_dict}
        line = _canonical(envelope) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        key = (record.market_state_hash, record.question_schema_version)
        self._index.setdefault(key, len(self._records))
        self._records.append(record)

    def find(self, *, market_state_hash: str,
             question_schema_version: str) -> PredictionRecord | None:
        index = self._index.get((market_state_hash, question_schema_version))
        return None if index is None else self._records[index]

    def __len__(self) -> int:
        return len(self._records)


__all__ = ["ARCHIVE_SCHEMA", "JsonlPredictionArchive", "PredictionArchiveError"]
