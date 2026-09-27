"""测试用确定性 fake：可控时钟、fake provider、合法响应构造。

所有预测测试都在无网络、无 Jev Key 的条件下运行（SC-10）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from market.events.types import Milliseconds
from prediction.providers.base import ProviderResponse
from prediction.schema.market_v1 import (
    PROBABILITY_QUESTIONS,
    QUESTION_SCHEMA_VERSION,
    horizon_key,
)
from prediction.types import FUTURE_RETURN_CATEGORIES, PredictionRequest

#: 均匀分布的合法概率，可按需覆盖。
DEFAULT_FLAT = 0.6
DEFAULT_SIDE = 0.1


class FakeClock:
    """可控单调时钟（毫秒）。预测层不使用 wall-clock。"""

    def __init__(self, now: Milliseconds = 0) -> None:
        self._now = now

    def now(self) -> Milliseconds:
        return self._now

    def advance(self, milliseconds: Milliseconds) -> Milliseconds:
        if milliseconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self._now += milliseconds
        return self._now


def response_payload(
    *,
    question_schema_version: str = QUESTION_SCHEMA_VERSION,
    flat: float = DEFAULT_FLAT,
    confidence: float | None = None,
    probabilities: dict[str, float] | None = None,
) -> dict[str, object]:
    """构造一个合法的 Jev 响应结构（测试可再局部篡改）。

    `flat` 之外的剩余概率均分给其余 4 个 bucket，保证求和为 1。
    """
    remaining = (1.0 - flat) / 2
    distribution = {
        "strong_down": remaining / 2,
        "down": remaining / 2,
        "flat": flat,
        "up": remaining / 2,
        "strong_up": remaining / 2,
    }
    payload: dict[str, object] = {
        "question_schema_version": question_schema_version,
        "future_return": {
            horizon_key(horizon): dict(distribution)
            for horizon in (5_000, 15_000, 30_000, 60_000)
        },
    }
    for question in PROBABILITY_QUESTIONS:
        payload[question] = probabilities[question] if probabilities else 0.5
    if confidence is not None:
        payload["confidence"] = confidence
    return payload


def response_json(**kwargs: object) -> str:
    """canonical-ish JSON 文本，便于写入 `raw_response`. """
    return json.dumps(response_payload(**kwargs), separators=(",", ":"))  # type: ignore[arg-type]


@dataclass
class FakeProvider:
    """确定性 provider。可注入原始响应、异常、gate 与模拟延迟。"""

    raw_response: str | None = None
    error: BaseException | None = None
    provider: str = "fake-jev"
    model: str = "fake-model-v1"
    clock: FakeClock | None = None
    latency_ms: Milliseconds = 0
    gate: asyncio.Event | None = None

    def __post_init__(self) -> None:
        self.requests: list[PredictionRequest] = []

    async def predict(self, request: PredictionRequest) -> ProviderResponse:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if self.clock is not None and self.latency_ms:
            self.clock.advance(self.latency_ms)
        raw = self.raw_response if self.raw_response is not None else response_json()
        return ProviderResponse(provider=self.provider, model=self.model, raw_response=raw)


__all__ = [
    "DEFAULT_FLAT",
    "DEFAULT_SIDE",
    "FUTURE_RETURN_CATEGORIES",
    "FakeClock",
    "FakeProvider",
    "response_json",
    "response_payload",
]
