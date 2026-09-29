"""Raw Facts drill-down（P0001.12.2 / G2）：把既有对象映射成**有界**的原始事实视图。

纪律：

- 只读搬运：不做任何解释、不重算交易事实；
- **有界**：长文本（例如 provider raw response）截断并显式标记 `truncated`；
- 不存在 ⇒ `RawFactView(available=False)`（调用方映射 404，而不是返回空对象冒充"没有事实"）；
- 不泄漏凭据：只暴露既有事实字段，不做任何额外读取。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from product.types import Fact

#: 单个文本字段的最大长度（截断必须显式标记）
MAX_TEXT_CHARS = 4_000

SUPPORTED_KINDS: tuple[str, ...] = ("order", "fill", "decision", "execution_event", "prediction")


@dataclass(frozen=True, slots=True)
class RawFactView:
    """一条原始事实（有界）。"""

    kind: str
    identity: str
    available: bool
    facts: tuple[tuple[str, Fact], ...] = ()
    notes: tuple[str, ...] = ()
    truncated_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Fact]:
        return {name: fact for name, fact in self.facts}


def _text(value: object) -> tuple[Fact, bool]:
    if value is None:
        return Fact.unknown("not_provided"), False
    text = str(value)
    if len(text) > MAX_TEXT_CHARS:
        return Fact.of(text[:MAX_TEXT_CHARS]), True
    return Fact.of(text), False


def raw_facts_for(kind: str, identity: str, obj: object | None) -> RawFactView:
    """把一个既有对象映射成有界原始事实（`obj is None` ⇒ 不可用）。"""
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported raw-fact kind {kind!r} (supported: {SUPPORTED_KINDS})")
    if not isinstance(identity, str) or not identity:
        raise ValueError("raw facts require a non-empty identity")
    if obj is None:
        return RawFactView(kind=kind, identity=identity, available=False,
                           notes=("object not found in the current runtime (UNKNOWN, not empty)",))
    get = lambda name: getattr(obj, name, None)  # noqa: E731 - 局部只读访问器
    fields: list[tuple[str, Fact]] = []
    truncated: list[str] = []

    def add(name: str, value: object) -> None:
        fact, was_truncated = _text(value)
        fields.append((name, fact))
        if was_truncated:
            truncated.append(name)

    if kind == "order":
        add("client_order_id", get("client_order_id"))
        add("symbol", get("symbol"))
        add("side", getattr(get("side"), "value", get("side")))
        add("status", getattr(get("status"), "value", get("status")))
        add("price", get("price"))
        add("quantity", get("quantity"))
        add("filled_quantity", get("filled_quantity"))
        add("reduce_only", get("reduce_only"))
        add("created_at", get("created_at"))
        add("updated_at", get("updated_at"))
    elif kind == "fill":
        add("client_order_id", get("client_order_id"))
        add("exchange_order_id", get("exchange_order_id"))
        add("trade_id", get("trade_id"))
        add("price", get("price"))
        add("quantity", get("quantity"))
        add("fee", get("fee"))
        add("fee_asset", get("fee_asset"))
        add("ts", get("ts"))
    elif kind == "decision":
        add("at_ms", get("at_ms"))
        add("mode", getattr(get("mode"), "value", get("mode")))
        add("detail", get("detail"))
        add("blocked_by", getattr(get("blocked_by"), "value", get("blocked_by")))
        add("bid_action", getattr(getattr(get("bid"), "action", None), "value", None))
        add("bid_price", getattr(get("bid"), "price", None))
        add("ask_action", getattr(getattr(get("ask"), "action", None), "value", None))
        add("ask_price", getattr(get("ask"), "price", None))
    elif kind == "execution_event":
        add("event_type", getattr(get("event_type"), "value", get("event_type")))
        add("client_order_id", get("client_order_id"))
        add("reason", get("reason"))
        add("timestamp", get("timestamp"))
    else:  # prediction
        add("request_id", get("request_id"))
        add("provider", get("provider"))
        add("model", get("model"))
        add("as_of", get("as_of"))
        add("expires_at", get("expires_at"))
        add("latency_ms", get("latency_ms"))
        add("market_state_hash", get("market_state_hash"))
        add("raw_response", get("raw_response"))
    return RawFactView(kind=kind, identity=identity, available=True, facts=tuple(fields),
                       notes=("read-only raw facts; long text fields are truncated and flagged",),
                       truncated_fields=tuple(truncated))


__all__ = ["MAX_TEXT_CHARS", "SUPPORTED_KINDS", "RawFactView", "raw_facts_for"]
