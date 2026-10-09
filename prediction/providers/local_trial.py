"""`LOCAL_TRIAL` **本地试验** prediction provider（人类裁决 2026-10-04 授权）。

性质与边界（严格按裁决）：

- 复用既有 `PredictionProvider` 契约与 `PredictionRuntime`，**不引入第二套预测契约**；
- 只允许**显式配置**的本地 REPLAY/PAPER 环境启用（`prediction.provider = "local_trial"`）；
  TESTNET/LIVE 一律拒绝（由 composition root 校验，见 `runtime/assembly.py`）；
- 输入**只来自**既有 canonical Jev payload（MarketState / FeatureEngine 事实）；
- 输出是严格合法的 `jev-market-v1` 响应 ⇒ 由既有 strict parser 解析（不自造解析路径）；
- 预测携带明确来源与试验标记：`provider="LOCAL_TRIAL"`、`model="local-trial-v1"`；
  UI / API / Assistant **不得**把它描述成真实模型预测；
- **不静默 fallback**：输入不足（缺 mid / 缺 depth / 无可用 return）直接抛错 ⇒ 既有 runtime 按 provider
  失败处理（fail closed），不产生预测。

## 可审计的规则（本文件即规则；常量固定、确定性、可复现）

1. 方向信号 `signal ∈ [-1, 1]`（先用 15s 收益，缺失则 5s，再缺失则 1s）：

   归一化收益 `mom = return_h / max(spread, eps)`（用点差归一，避免自造波动率模型）；
   `signal = clamp(0.50*mom + 0.25*l1_imbalance + 0.15*normalized_ofi_5s + 0.10*microprice_skew, -1, 1)`
   其中 `microprice_skew = clamp((microprice - mid) / (spread/2), -1, 1)`。

2. 每个 horizon 的 5 分类分布：基础形状
   `flat = 0.40`，其余 0.60 按 `down = strong_down = up = strong_up = 0.15`；
   然后按 `shift = MAX_SHIFT * |signal| * HORIZON_WEIGHT[h]` 把概率从**反向**两桶移到**同向**两桶
   （`strong_*` 拿移量的 `STRONG_FRACTION`），最后归一化并四舍五入到 1e-6，保证和恰为 1。

3. 四个单概率问题：
   - `buy_adverse_selection = 0.5 + 0.25 * clamp(normalized_ofi_5s)`
   - `sell_adverse_selection = 0.5 - 0.25 * clamp(normalized_ofi_5s)`
   - `buy_fill_probability = 0.5 + 0.30 * clamp(l1_imbalance)`
   - `sell_fill_probability = 0.5 - 0.30 * clamp(l1_imbalance)`
   - `confidence = |signal|`（试验模型自报置信，**不是**真实模型置信）
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field

from prediction.errors import PredictionProviderError
from prediction.providers.base import ProviderResponse
from prediction.schema.market_v1 import (FUTURE_RETURN_HORIZONS_MS, PROBABILITY_QUESTIONS,
                                         QUESTION_SCHEMA_VERSION, horizon_key)
from prediction.types import FUTURE_RETURN_CATEGORIES, PredictionRequest

#: 试验标记（UI/API/Assistant 据此标注，不得描述为真实模型）。
LOCAL_TRIAL_PROVIDER_ID = "LOCAL_TRIAL"
LOCAL_TRIAL_MODEL_ID = "local-trial-v1"

#: 规则常量（固定、可审计）
FLAT_MASS = 0.40
BASE_TAIL = 0.15
MAX_SHIFT = 0.30
STRONG_FRACTION = 0.40
HORIZON_WEIGHT: dict[int, float] = {5_000: 0.40, 15_000: 0.60, 30_000: 0.80, 60_000: 1.00}
EPSILON = 1e-12
ROUND_TO = 1e-6


class LocalTrialInputError(PredictionProviderError):
    """输入事实不足 ⇒ fail closed（不产生任何预测，也不回退到别的 provider）。"""


def _clamp(value: float, *, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(payload: dict[str, object], section: str, key: str) -> float | None:
    block = payload.get(section)
    if not isinstance(block, dict):
        return None
    value = block.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass
class LocalTrialProvider:
    """确定性本地试验 provider（仅 REPLAY/PAPER；由 composition root 显式启用）。"""

    #: 审计：最近若干次决策的信号与输入摘要（可审计，不是业务状态）
    audit_capacity: int = 64
    _audit: deque = field(default_factory=deque, init=False)
    requests: list[PredictionRequest] = field(default_factory=list, init=False)

    provider_id: str = LOCAL_TRIAL_PROVIDER_ID
    model_id: str = LOCAL_TRIAL_MODEL_ID

    # ------------------------------------------------------------------ 契约

    async def predict(self, request: PredictionRequest) -> ProviderResponse:
        self.requests.append(request)
        payload = _load_payload(request)
        signal, inputs = self._signal(payload)
        distribution = {horizon: self._distribution(signal, horizon_ms=horizon)
                        for horizon in FUTURE_RETURN_HORIZONS_MS}
        answers = self._probability_answers(payload, signal)
        body: dict[str, object] = {
            "question_schema_version": QUESTION_SCHEMA_VERSION,
            "future_return": {horizon_key(horizon): distribution[horizon]
                              for horizon in FUTURE_RETURN_HORIZONS_MS},
            **answers,
            "confidence": abs(signal),
        }
        self._audit.append({"request_id": request.request_id, "signal": round(signal, 6),
                            "inputs": inputs, "horizon_weight": dict(HORIZON_WEIGHT)})
        while len(self._audit) > self.audit_capacity:
            self._audit.popleft()
        return ProviderResponse(provider=self.provider_id, model=self.model_id,
                                raw_response=json.dumps(body, separators=(",", ":"), sort_keys=True),
                                requested_model=self.model_id, resolved_model=self.model_id,
                                response_id=f"local-trial-{request.request_id}")

    # ------------------------------------------------------------------ 规则

    def _signal(self, payload: dict[str, object]) -> tuple[float, dict[str, float | None]]:
        mid = _number(payload, "price", "mid")
        spread = _number(payload, "price", "spread")
        microprice = _number(payload, "price", "microprice")
        imbalance = _number(payload, "depth", "l1_imbalance")
        ofi = _number(payload, "flow", "normalized_ofi_5s")
        if mid is None or spread is None:
            # 输入不足 ⇒ fail closed（不猜、不回退）
            raise LocalTrialInputError(
                "LOCAL_TRIAL requires price mid/spread facts; refusing to predict without them")
        if imbalance is None and ofi is None:
            raise LocalTrialInputError(
                "LOCAL_TRIAL requires depth imbalance or normalized flow; refusing to predict without them")

        horizon, ret = self._first_return(payload)
        if ret is None:
            raise LocalTrialInputError(
                "LOCAL_TRIAL requires at least one realized return fact; refusing to predict without it")
        mom = _clamp(ret / max(spread, EPSILON))
        skew: float | None = None
        if microprice is not None:
            skew = _clamp((microprice - mid) / max(spread / 2.0, EPSILON))
        signal = _clamp(0.50 * mom
                        + 0.25 * (imbalance or 0.0)
                        + 0.15 * _clamp(ofi or 0.0)
                        + 0.10 * (skew or 0.0))
        return signal, {"return_used": f"return_{horizon // 1000}s", "return": ret, "mom": mom,
                        "l1_imbalance": imbalance, "normalized_ofi_5s": ofi, "microprice_skew": skew,
                        "mid": mid, "spread": spread}

    @staticmethod
    def _first_return(payload: dict[str, object]) -> tuple[int, float | None]:
        for seconds in (15, 5, 1):
            value = _number(payload, "returns", f"return_{seconds}s")
            if value is not None:
                return seconds * 1_000, value
        return 0, None

    def _distribution(self, signal: float, *, horizon_ms: int) -> dict[str, float]:
        weight = HORIZON_WEIGHT.get(horizon_ms, 0.0)
        shift = MAX_SHIFT * abs(signal) * weight
        if signal >= 0:
            take_from, give_to = ("strong_down", "down"), ("up", "strong_up")
        else:
            take_from, give_to = ("up", "strong_up"), ("down", "strong_down")
        values = {"strong_down": BASE_TAIL, "down": BASE_TAIL, "flat": FLAT_MASS,
                  "up": BASE_TAIL, "strong_up": BASE_TAIL}
        for name in take_from:
            values[name] = max(0.0, values[name] - shift / 2.0)
        values[give_to[0]] += shift * (1.0 - STRONG_FRACTION)
        values[give_to[1]] += shift * STRONG_FRACTION
        total = sum(values.values())
        rounded = {name: round(values[name] / total, 6) for name in FUTURE_RETURN_CATEGORIES}
        # 四舍五入后修正尾差，保证和恰为 1（strict parser 有求和校验）
        drift = round(1.0 - sum(rounded.values()), 6)
        rounded["flat"] = round(rounded["flat"] + drift, 6)
        return rounded

    @staticmethod
    def _probability_answers(payload: dict[str, object], signal: float) -> dict[str, float]:
        imbalance = _number(payload, "depth", "l1_imbalance") or 0.0
        ofi = _clamp(_number(payload, "flow", "normalized_ofi_5s") or 0.0)
        answers = {
            "buy_adverse_selection": _clamp(0.5 + 0.25 * ofi, low=0.0, high=1.0),
            "sell_adverse_selection": _clamp(0.5 - 0.25 * ofi, low=0.0, high=1.0),
            "buy_fill_probability": _clamp(0.5 + 0.30 * _clamp(imbalance), low=0.0, high=1.0),
            "sell_fill_probability": _clamp(0.5 - 0.30 * _clamp(imbalance), low=0.0, high=1.0),
        }
        assert set(answers) == set(PROBABILITY_QUESTIONS), "answers must cover every question"
        return answers

    # ------------------------------------------------------------------ 审计

    def audit_log(self) -> tuple[dict[str, object], ...]:
        """可审计的决策摘要（只读；供 tests/ops 检查规则如何被应用）。"""
        return tuple(self._audit)


def _load_payload(request: PredictionRequest) -> dict[str, object]:
    if not isinstance(request, PredictionRequest):
        raise LocalTrialInputError("LOCAL_TRIAL requires a PredictionRequest")
    try:
        payload = json.loads(request.payload_json)
    except json.JSONDecodeError as exc:
        raise LocalTrialInputError(f"canonical payload is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise LocalTrialInputError("canonical payload must be a JSON object")
    return payload


__all__ = ["FLAT_MASS", "HORIZON_WEIGHT", "LOCAL_TRIAL_MODEL_ID", "LOCAL_TRIAL_PROVIDER_ID",
           "LocalTrialInputError", "LocalTrialProvider"]
