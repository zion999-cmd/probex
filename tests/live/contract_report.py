"""真实 Jev content 的契约诊断（live test 的只读报告器）。

用途：把真实返回的 Jev content 与当前 `jev-market-v1` 契约逐项对照，输出结构化差异，
而不是静默修值或模糊兼容。本模块只做**报告**，不做转换：

- OpenRouter envelope 结构；
- Jev content 的顶层键与类型；
- 旧 FMZ V3 领域语义是否仍存在：`market_*`（choice / probabilities / confidence）、
  `toxicity_*`（score / probabilities）、`fill_*`（noul / probability / value）；
- 当前 `jev-market-v1` 契约：`future_return.<horizon>.<category>` 五分类是否存在、
  是否互斥、求和是否为 1，以及四个单概率问题是否存在；
- 调用 latency。

输出为纯文本报告，可直接贴进 `context/handoff.md` 或提案修订。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from prediction.schema.market_v1 import (
    FUTURE_RETURN_HORIZONS_MS,
    PROBABILITY_QUESTIONS,
    PROBABILITY_SUM_TOLERANCE,
    horizon_key,
)
from prediction.types import FUTURE_RETURN_CATEGORIES

#: 旧 FMZ V3 领域语义的探测目标（仅作诊断，不作为契约）。
LEGACY_FAMILIES: dict[str, tuple[str, ...]] = {
    "market_": ("choice", "probabilities", "confidence"),
    "toxicity_": ("score", "probabilities", "confidence"),
    "fill_": ("noul", "probability", "value"),
}


@dataclass
class ContractReport:
    """一次真实调用的契约诊断结果。"""

    envelope_ok: bool
    envelope_keys: tuple[str, ...] = ()
    content_keys: tuple[str, ...] = ()
    content_is_json_object: bool = False
    legacy_families: dict[str, dict[str, str]] = field(default_factory=dict)
    future_return_present: bool = False
    future_return_horizons: tuple[str, ...] = ()
    future_return_missing_horizons: tuple[str, ...] = ()
    future_return_missing_categories: dict[str, tuple[str, ...]] = field(default_factory=dict)
    probability_sums: dict[str, float] = field(default_factory=dict)
    probability_question_presence: dict[str, bool] = field(default_factory=dict)
    confidence_present: bool = False
    latency_ms: int | None = None
    mismatches: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def matches_jev_market_v1(self) -> bool:
        return not self.mismatches

    def render(self) -> str:
        lines = [
            "=== JEV CONTRACT REPORT ==",
            f"envelope_ok: {self.envelope_ok}",
            f"envelope_keys: {list(self.envelope_keys)}",
            f"content_is_json_object: {self.content_is_json_object}",
            f"content_keys: {list(self.content_keys)}",
        ]
        for family, findings in self.legacy_families.items():
            lines.append(f"legacy[{family}]: {findings}")
        lines.extend(
            [
                f"future_return_present: {self.future_return_present}",
                f"future_return_horizons: {list(self.future_return_horizons)}",
                f"future_return_missing_horizons: {list(self.future_return_missing_horizons)}",
                f"future_return_missing_categories: {self.future_return_missing_categories}",
                f"probability_sums: {self.probability_sums}",
                f"probability_questions: {self.probability_question_presence}",
                f"confidence_present: {self.confidence_present}",
                f"latency_ms: {self.latency_ms}",
                f"matches_jev_market_v1: {self.matches_jev_market_v1}",
                f"mismatches: {list(self.mismatches)}",
            ]
        )
        if self.notes:
            lines.append(f"notes: {list(self.notes)}")
        return "\n".join(lines)


def describe_envelope(response_body: str) -> tuple[bool, tuple[str, ...], str]:
    """解析 OpenRouter 外层并返回 (是否成功, 顶层键, content)。"""
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError:
        return False, (), ""
    if not isinstance(parsed, dict):
        return False, (), ""
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False, tuple(sorted(parsed)), ""
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        return False, tuple(sorted(parsed)), ""
    return True, tuple(sorted(parsed)), message["content"]


def describe_content(
    content: str,
    *,
    latency_ms: int | None = None,
    envelope_ok: bool = True,
    envelope_keys: tuple[str, ...] = (),
) -> ContractReport:
    """把 Jev content 与 `jev-market-v1` 契约对照。"""
    report = ContractReport(
        envelope_ok=envelope_ok,
        envelope_keys=envelope_keys,
        latency_ms=latency_ms,
    )
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        report.mismatches = (f"content is not valid JSON: {exc}",)
        return report

    if not isinstance(parsed, dict):
        report.mismatches = (f"content must be a JSON object, got {type(parsed).__name__}",)
        return report

    report.content_is_json_object = True
    report.content_keys = tuple(sorted(parsed))

    for prefix, fields in LEGACY_FAMILIES.items():
        findings: dict[str, str] = {}
        for key, value in parsed.items():
            if key.startswith(prefix) and isinstance(value, dict):
                present = [name for name in fields if name in value]
                absent = [name for name in fields if name not in value]
                findings[key] = f"present={present} absent={absent} keys={sorted(value)}"
        if findings:
            report.legacy_families[prefix] = findings

    _describe_future_return(parsed.get("future_return"), report)
    for question in PROBABILITY_QUESTIONS:
        present = question in parsed
        report.probability_question_presence[question] = present
        if not present:
            report.mismatches += (f"missing probability question {question!r}",)
        elif not isinstance(parsed[question], (int, float)) or isinstance(parsed[question], bool):
            report.mismatches += (f"{question!r} is not a number",)

    report.confidence_present = "confidence" in parsed
    if report.confidence_present:
        report.notes += ("content carries provider confidence",)
    return report


def _describe_future_return(raw: object, report: ContractReport) -> None:
    if not isinstance(raw, dict):
        report.mismatches += (
            "content has no `future_return` object (旧 FMZ 语义的 market_* 不能直接映射为 jev-market-v1)",
        )
        return

    report.future_return_present = True
    report.future_return_horizons = tuple(sorted(raw))
    expected_keys = {horizon_key(horizon): horizon for horizon in FUTURE_RETURN_HORIZONS_MS}
    missing = tuple(key for key in expected_keys if key not in raw)
    if missing:
        report.future_return_missing_horizons = missing
        report.mismatches += (f"future_return missing horizons {list(missing)}",)
    extra = tuple(key for key in raw if key not in expected_keys)
    if extra:
        report.mismatches += (f"future_return has unexpected horizons {list(extra)}",)

    for key, value in raw.items():
        if not isinstance(value, dict):
            report.mismatches += (f"future_return.{key} is not an object",)
            continue
        missing_categories = tuple(category for category in FUTURE_RETURN_CATEGORIES if category not in value)
        if missing_categories:
            report.future_return_missing_categories[key] = missing_categories
            report.mismatches += (f"future_return.{key} missing categories {list(missing_categories)}",)
            continue
        extra_categories = tuple(category for category in value if category not in FUTURE_RETURN_CATEGORIES)
        if extra_categories:
            report.mismatches += (f"future_return.{key} has unexpected categories {list(extra_categories)}",)
        try:
            total = float(sum(value[category] for category in FUTURE_RETURN_CATEGORIES))
        except (TypeError, ValueError):
            report.mismatches += (f"future_return.{key} has non-numeric probabilities",)
            continue
        report.probability_sums[key] = total
        if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
            report.mismatches += (
                f"future_return.{key} probabilities sum to {total} (tolerance {PROBABILITY_SUM_TOLERANCE})",
            )


__all__ = [
    "LEGACY_FAMILIES",
    "ContractReport",
    "describe_content",
    "describe_envelope",
]
