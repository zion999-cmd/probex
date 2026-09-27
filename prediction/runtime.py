"""PredictionRuntime：把 MarketState 变成可审计的 PredictionRecord。

关键纪律（P0001.4）：

- Jev 调用与 Market Feed 完全解耦：runtime 只接收一个 immutable `MarketState`，
  调用期间市场可以继续更新（调用方继续喂 `FeatureEngine`），runtime 不持有引擎。
- 失败绝不伪造预测：超时 / 传输失败 / provider 异常 / 解析失败都不产生 record。
- 迟到旧结果只被记录（`STALE_RESPONSE`），不会成为 `latest_prediction`。
- `RECORDED` 模式只查历史记录，绝不调用 provider（不隐式访问网络）。
- 时间全部来自注入的 `Clock`，本模块不使用 wall-clock。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

from market.events.types import Milliseconds
from market.state.types import MarketState
from prediction.errors import (
    PredictionError,
    PredictionInvalidResponseError,
    PredictionParseError,
    PredictionProviderError,
    PredictionTimeoutError,
    PredictionTransportError,
)
from prediction.parsing.market_v1 import parse_jev_prediction
from prediction.providers.base import PredictionProvider, ProviderResponse
from prediction.scheduler import PredictionScheduler
from prediction.schema.market_v1 import (
    QUESTION_SCHEMA_VERSION,
    build_prediction_request,
    market_state_hash,
)
from prediction.types import (
    Clock,
    InMemoryPredictionArchive,
    Prediction,
    PredictionArchive,
    PredictionMode,
    PredictionOutcome,
    PredictionRecord,
    PredictionRequest,
    PredictionResult,
    ProviderStatus,
)

#: backoff 默认参数（可由构造参数覆盖）。
DEFAULT_BASE_BACKOFF_MS: Milliseconds = 1_000
DEFAULT_MAX_BACKOFF_MS: Milliseconds = 60_000

_ERROR_OUTCOMES: Mapping[type[PredictionError], PredictionOutcome] = {
    PredictionTimeoutError: PredictionOutcome.TIMEOUT,
    PredictionTransportError: PredictionOutcome.TRANSPORT_ERROR,
    PredictionProviderError: PredictionOutcome.PROVIDER_ERROR,
    PredictionParseError: PredictionOutcome.PARSE_ERROR,
    PredictionInvalidResponseError: PredictionOutcome.INVALID_RESPONSE,
}


def outcome_for_error(error: PredictionError) -> PredictionOutcome:
    """把失败类型映射为结果分类。"""
    for error_type, outcome in _ERROR_OUTCOMES.items():
        if isinstance(error, error_type):
            return outcome
    return PredictionOutcome.PROVIDER_ERROR


class PredictionRuntime:
    """单一 (venue, symbol, question schema) 的预测运行时。"""

    def __init__(
        self,
        *,
        provider: PredictionProvider,
        clock: Clock,
        timeout_ms: Milliseconds,
        ttl_ms: Milliseconds,
        mode: PredictionMode = PredictionMode.LIVE_REQUERY,
        archive: PredictionArchive | None = None,
        scheduler: PredictionScheduler | None = None,
        max_inflight: int = 1,
        base_backoff_ms: Milliseconds = DEFAULT_BASE_BACKOFF_MS,
        max_backoff_ms: Milliseconds = DEFAULT_MAX_BACKOFF_MS,
        question_schema_version: str = QUESTION_SCHEMA_VERSION,
    ) -> None:
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")
        if ttl_ms <= 0:
            raise ValueError("ttl_ms must be > 0")
        if base_backoff_ms <= 0 or max_backoff_ms <= 0:
            raise ValueError("backoff values must be > 0")
        if max_backoff_ms < base_backoff_ms:
            raise ValueError("max_backoff_ms must be >= base_backoff_ms")

        self._provider = provider
        self._clock = clock
        self._timeout_ms = timeout_ms
        self._ttl_ms = ttl_ms
        self._mode = mode
        self._archive = archive if archive is not None else InMemoryPredictionArchive()
        self._scheduler = scheduler if scheduler is not None else PredictionScheduler(max_inflight=max_inflight)
        self._base_backoff_ms = base_backoff_ms
        self._max_backoff_ms = max_backoff_ms
        self._question_schema_version = question_schema_version

        self._consecutive_failures = 0
        self._next_retry_at: Milliseconds | None = None
        self._submissions: list[PredictionResult] = []

    @property
    def mode(self) -> PredictionMode:
        return self._mode

    @property
    def scheduler(self) -> PredictionScheduler:
        return self._scheduler

    @property
    def archive(self) -> PredictionArchive:
        return self._archive

    @property
    def latest_prediction(self) -> PredictionRecord | None:
        return self._scheduler.latest_accepted

    @property
    def submissions(self) -> tuple[PredictionResult, ...]:
        """全部提交结果（含被跳过、失败、迟到），用于审计。"""
        return tuple(self._submissions)

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def next_retry_at(self) -> Milliseconds | None:
        return self._next_retry_at

    @property
    def provider_status(self) -> ProviderStatus:
        return self.status_at(self._clock.now())

    def status_at(self, at: Milliseconds) -> ProviderStatus:
        """在 `at` 时刻的 provider 状态。"""
        if self._consecutive_failures == 0:
            return ProviderStatus.HEALTHY
        if self._next_retry_at is not None and at < self._next_retry_at:
            return ProviderStatus.BACKING_OFF
        return ProviderStatus.DEGRADED

    def is_expired(self, record: PredictionRecord) -> bool:
        """该记录在当前时刻是否已过期。"""
        return record.is_expired(self._clock.now())

    async def submit(self, state: MarketState) -> PredictionResult:
        """针对一个 MarketState 产生预测结果。"""
        now = self._clock.now()
        if self._mode is PredictionMode.RECORDED:
            return self._submit_recorded(state, now)

        admission = self._admit(state, now)
        if isinstance(admission, PredictionResult):
            return admission
        request = admission

        response = await self._call_provider(request)
        if isinstance(response, PredictionError):
            return self._failed(request, response)

        try:
            prediction = parse_jev_prediction(
                response.raw_response,
                expected_question_schema_version=request.question_schema_version,
            )
        except PredictionError as exc:
            return self._failed(request, exc)

        return self._complete(request, response, prediction)

    def _admit(self, state: MarketState, now: Milliseconds) -> PredictionRequest | PredictionResult:
        """资格 / backoff / inflight 闸门：通过则返回冻结好的请求，否则返回跳过结果。"""
        if not self._scheduler.check(state).eligible:
            return self._skipped(PredictionOutcome.NOT_ELIGIBLE, now)
        if self.status_at(now) is ProviderStatus.BACKING_OFF:
            return self._skipped(PredictionOutcome.SKIPPED_BACKOFF, now)

        sequence = self._scheduler.reserve()
        if sequence is None:
            return self._skipped(PredictionOutcome.SKIPPED_INFLIGHT, now)

        return build_prediction_request(
            state,
            sequence=sequence,
            created_at=now,
            ttl_ms=self._ttl_ms,
            question_schema_version=self._question_schema_version,
        )

    def _skipped(self, outcome: PredictionOutcome, now: Milliseconds) -> PredictionResult:
        return self._log(
            PredictionResult(outcome=outcome, request=None, record=None, error=None, completed_at=now)
        )

    def _complete(
        self,
        request: PredictionRequest,
        response: ProviderResponse,
        prediction: Prediction,
    ) -> PredictionResult:
        """成功路径：落记录、交给调度层判定是否成为 current prediction。"""
        received_at = self._clock.now()
        record = self._build_record(request, response, prediction, received_at)
        outcome = self._scheduler.accept(request=request, record=record)
        if outcome is PredictionOutcome.ACCEPTED:
            self._note_success()
            self._archive.store(record)

        return self._log(
            PredictionResult(
                outcome=outcome,
                request=request,
                record=record,
                error=None,
                completed_at=received_at,
            )
        )

    async def _call_provider(self, request: PredictionRequest) -> ProviderResponse | PredictionError:
        """调用 provider，把一切失败收敛为错误对象（不抛出）。"""
        try:
            return await asyncio.wait_for(
                self._provider.predict(request),
                timeout=self._timeout_ms / 1_000,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return PredictionTimeoutError(f"provider did not respond within {self._timeout_ms} ms")
        except PredictionError as exc:
            return exc
        except Exception as exc:  # provider 契约外的异常同样 fail closed
            return PredictionProviderError(f"provider raised {type(exc).__name__}: {exc}")

    def _build_record(
        self,
        request: PredictionRequest,
        response: ProviderResponse,
        prediction: Prediction,
        received_at: Milliseconds,
    ) -> PredictionRecord:
        return PredictionRecord(
            request_id=request.request_id,
            sequence=request.sequence,
            market_state_hash=request.market_state_hash,
            feature_schema_version=request.feature_schema_version,
            question_schema_version=request.question_schema_version,
            provider=response.provider,
            model=response.model,
            mode=self._mode,
            as_of=request.as_of,
            request_created_at=request.created_at,
            response_received_at=received_at,
            latency_ms=max(0, received_at - request.created_at),
            expires_at=request.expires_at,
            raw_response=response.raw_response,
            prediction=prediction,
        )

    def _submit_recorded(self, state: MarketState, now: Milliseconds) -> PredictionResult:
        record = self._archive.find(
            market_state_hash=market_state_hash(state),
            question_schema_version=self._question_schema_version,
        )
        if record is None:
            return self._log(
                PredictionResult(
                    outcome=PredictionOutcome.NOT_RECORDED,
                    request=None,
                    record=None,
                    error=None,
                    completed_at=now,
                )
            )
        self._scheduler.record_replayed(record)
        return self._log(
            PredictionResult(
                outcome=PredictionOutcome.ACCEPTED,
                request=None,
                record=replace(record),
                error=None,
                completed_at=now,
            )
        )

    def _failed(self, request: PredictionRequest, error: PredictionError) -> PredictionResult:
        self._scheduler.abandon()
        completed_at = self._clock.now()
        self._note_failure(completed_at)
        return self._log(
            PredictionResult(
                outcome=outcome_for_error(error),
                request=request,
                record=None,
                error=error,
                completed_at=completed_at,
            )
        )

    def _note_success(self) -> None:
        self._consecutive_failures = 0
        self._next_retry_at = None

    def _note_failure(self, at: Milliseconds) -> None:
        self._consecutive_failures += 1
        backoff = min(self._base_backoff_ms * (2 ** (self._consecutive_failures - 1)), self._max_backoff_ms)
        self._next_retry_at = at + backoff

    def _log(self, result: PredictionResult) -> PredictionResult:
        self._submissions.append(result)
        return result


__all__ = [
    "DEFAULT_BASE_BACKOFF_MS",
    "DEFAULT_MAX_BACKOFF_MS",
    "PredictionRuntime",
    "outcome_for_error",
]
