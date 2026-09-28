"""System One wire 契约（provider 与 parser 共用的单一事实来源）。

这里集中定义**请求/响应的线上形状**，避免「构造请求的命名」与「解析响应的命名」各自漂移：

- 端点 / 模型别名 / 环境变量 / timeout
- question id 命名（`market_5s`…、`buy_fill`…）
- typed 问题构造（Choice / Noul）与 `state` / 请求体构造
- 响应字段名（`answers` / `choice` / `probabilities` / `confidence` / `noul` / `usage` …）

本模块不做 HTTP、不做解析、不接触凭证。事实来源：`docs.typesafe.ai/api`、
`docs.typesafe.ai/primitives/choice`、`docs.typesafe.ai/primitives/noul`，
以及 P0001.4.1 P2 实测（`{"type":"noul","noul":0.99}`）。

注意：这些是 **provider wire schema**，不是 domain schema。`jev-market-v1` 的业务语义不变。
"""

from __future__ import annotations

import json

from prediction.schema.market_v1 import horizon_key

#: OpenRouter 上的 System One 端点（base + path 可覆盖，但默认固定）。
SYSTEMONE_BASE_URL = "https://openrouter.ai/api"
SYSTEMONE_PATH = "/v1/systemone"

#: 请求使用 alias；resolved model 由响应给出并写入记录，不硬编码。
SYSTEMONE_MODEL_ALIAS = "jev-1.13"

#: 凭证环境变量（Key 不落盘）。
SYSTEMONE_API_KEY_ENV = "OPENROUTER_API_KEY"

#: 默认 HTTP timeout（秒）。
DEFAULT_TIMEOUT_S = 60.0

#: 未来收益五分类问题的 id 前缀。
FUTURE_RETURN_QUESTION_PREFIX = "market_"

#: 二元问题的固定 horizon（提案：adverse selection 用 5 秒）。
BINARY_QUESTION_HORIZON_MS = 5_000

#: 二元问题：domain question 名 → wire question id。
BUY_ADVERSE_SELECTION_QUESTION = "buy_adverse_selection"
SELL_ADVERSE_SELECTION_QUESTION = "sell_adverse_selection"
BUY_FILL_QUESTION = "buy_fill"
SELL_FILL_QUESTION = "sell_fill"

#: 响应中 answers 的字段名。
ANSWER_TYPE_FIELD = "type"
CHOICE_FIELD = "choice"
PROBABILITIES_FIELD = "probabilities"
CONFIDENCE_FIELD = "confidence"
NOUL_FIELD = "noul"

#: 五分类 bucket 的定性描述（提案未定义数值分档 → 不自行编造 bps 阈值）。
BUCKET_CRITERIA: dict[str, str] = {
    "strong_down": "a large downward move: the mid price ends clearly below its current value",
    "down": "a moderate downward move",
    "flat": "little or no net change in the mid price",
    "up": "a moderate upward move",
    "strong_up": "a large upward move: the mid price ends clearly above its current value",
}

#: domain 二元问题（`Prediction` 字段名）→ wire question id。
BINARY_QUESTION_WIRE_IDS: dict[str, str] = {
    "buy_adverse_selection": BUY_ADVERSE_SELECTION_QUESTION,
    "sell_adverse_selection": SELL_ADVERSE_SELECTION_QUESTION,
    "buy_fill_probability": BUY_FILL_QUESTION,
    "sell_fill_probability": SELL_FILL_QUESTION,
}


def future_return_question_id(horizon_ms: int) -> str:
    """`5000 -> "market_5s"`。"""
    return f"{FUTURE_RETURN_QUESTION_PREFIX}{horizon_key(horizon_ms)}"


def future_return_question_ids(horizons_ms: tuple[int, ...]) -> tuple[str, ...]:
    return tuple(future_return_question_id(horizon) for horizon in horizons_ms)


def build_future_return_question(horizon_ms: int) -> dict[str, object]:
    """五分类未来收益 → 原生 Choice 问题。"""
    seconds = horizon_ms // 1_000
    return {
        "type": "choice",
        "instructions": (
            f"Using the market state above, how will the mid price move over the next {seconds} seconds "
            "relative to its current value? The five options are ordered from the largest fall "
            "(strong_down) to the largest rise (strong_up); flat means no material net move. "
            "Use `price`, `depth`, `flow`, `returns` and `volatility` as evidence."
        ),
        "criteria": dict(BUCKET_CRITERIA),
    }


def build_adverse_selection_question(side: str, *, threshold_bps: float, horizon_ms: int) -> dict[str, object]:
    """adverse selection → 原生 Noul（明确的二值事件，不用 Score）。"""
    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    if not isinstance(threshold_bps, (int, float)) or isinstance(threshold_bps, bool) or threshold_bps <= 0:
        raise ValueError("threshold_bps must be a positive number")
    seconds = horizon_ms // 1_000
    level = "bid" if side == "buy" else "ask"
    adverse = "falls" if side == "buy" else "rises"
    direction = "lower" if side == "buy" else "higher"
    return {
        "type": "noul",
        "instructions": (
            f"If a passive limit {side} order resting now at the current best {level} "
            f"(`price.best_{level}`) were filled immediately, would the mid price be more than "
            f"{threshold_bps} bps {direction} {seconds} seconds later? "
            "Judge from `price`, `depth`, `flow`, `returns` and `volatility`."
        ),
        "criteria": {
            "true": (
                f"the mid price {seconds} seconds later is more than {threshold_bps} bps {direction} than the "
                f"current mid price (adverse selection: the mid {adverse} after the fill)"
            ),
            "false": (
                f"the mid price {seconds} seconds later is unchanged, moves the other way, or moves "
                f"{direction} by {threshold_bps} bps or less"
            ),
        },
    }


def build_fill_question(side: str, *, horizon_ms: int) -> dict[str, object]:
    """被动成交概率 → 原生 Noul。"""
    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    seconds = horizon_ms // 1_000
    level = "bid" if side == "buy" else "ask"
    return {
        "type": "noul",
        "instructions": (
            f"Would a passive limit {side} order resting now at the current best {level} "
            f"(`price.best_{level}`) be filled within the next {seconds} seconds, given the resting depth and "
            "the recent order flow in the state?"
        ),
        "criteria": {
            "true": f"the order is filled within {seconds} seconds",
            "false": f"the order is still unfilled after {seconds} seconds",
        },
    }


def build_questions(
    *,
    horizons_ms: tuple[int, ...],
    adverse_selection_threshold_bps: float,
) -> dict[str, dict[str, object]]:
    """一次请求的全部 typed 问题：4 Choice + 4 Noul。"""
    questions: dict[str, dict[str, object]] = {
        future_return_question_id(horizon): build_future_return_question(horizon) for horizon in horizons_ms
    }
    questions[BUY_ADVERSE_SELECTION_QUESTION] = build_adverse_selection_question(
        "buy", threshold_bps=adverse_selection_threshold_bps, horizon_ms=BINARY_QUESTION_HORIZON_MS
    )
    questions[SELL_ADVERSE_SELECTION_QUESTION] = build_adverse_selection_question(
        "sell", threshold_bps=adverse_selection_threshold_bps, horizon_ms=BINARY_QUESTION_HORIZON_MS
    )
    questions[BUY_FILL_QUESTION] = build_fill_question("buy", horizon_ms=BINARY_QUESTION_HORIZON_MS)
    questions[SELL_FILL_QUESTION] = build_fill_question("sell", horizon_ms=BINARY_QUESTION_HORIZON_MS)
    return questions


def build_systemone_state(domain_payload_json: str) -> dict[str, object]:
    """`state` = domain payload 去掉 `questions`（问题由 `questions` 字段单独承载）。"""
    try:
        payload = json.loads(domain_payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"domain payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("domain payload must be a JSON object")
    return {key: value for key, value in payload.items() if key != "questions"}


def build_systemone_body(
    domain_payload_json: str,
    *,
    model: str = SYSTEMONE_MODEL_ALIAS,
    horizons_ms: tuple[int, ...],
    adverse_selection_threshold_bps: float,
) -> str:
    """构建 System One 请求体（canonical JSON，便于审计与重放）。"""
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    if not horizons_ms:
        raise ValueError("horizons_ms must not be empty")
    body = {
        "model": model,
        "state": build_systemone_state(domain_payload_json),
        "questions": build_questions(
            horizons_ms=horizons_ms,
            adverse_selection_threshold_bps=adverse_selection_threshold_bps,
        ),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


__all__ = [
    "ANSWER_TYPE_FIELD",
    "BINARY_QUESTION_HORIZON_MS",
    "BINARY_QUESTION_WIRE_IDS",
    "BUCKET_CRITERIA",
    "BUY_ADVERSE_SELECTION_QUESTION",
    "BUY_FILL_QUESTION",
    "CHOICE_FIELD",
    "CONFIDENCE_FIELD",
    "DEFAULT_TIMEOUT_S",
    "FUTURE_RETURN_QUESTION_PREFIX",
    "NOUL_FIELD",
    "PROBABILITIES_FIELD",
    "SELL_ADVERSE_SELECTION_QUESTION",
    "SELL_FILL_QUESTION",
    "SYSTEMONE_API_KEY_ENV",
    "SYSTEMONE_BASE_URL",
    "SYSTEMONE_MODEL_ALIAS",
    "SYSTEMONE_PATH",
    "build_adverse_selection_question",
    "build_fill_question",
    "build_future_return_question",
    "build_questions",
    "build_systemone_body",
    "build_systemone_state",
    "future_return_question_id",
    "future_return_question_ids",
]
