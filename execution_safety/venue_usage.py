"""Venue **usage** 采集（closure Slice 3 / F-03 收口）。

与 `VenueLimitDefinition`（**定义**，来自 exchangeInfo/rateLimits）严格分开：

- 本模块只采**真实响应头**里的当前用量（`X-MBX-USED-WEIGHT-*`、`X-MBX-ORDER-COUNT-*`）；
- 每条观测记录**检查过哪个 endpoint、看到了哪些 header、是否解析出用量** ⇒
  即使某 endpoint 不返回用量头，也能证明"collector 已真实运行并检查过响应"（保留 UNKNOWN 但不再是"没接线"）；
- 绝不允许 operator 手填"当前使用量"冒充 runtime fact：usage 只能由 `observe(...)` 从头里解析。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from execution_safety.venue import VenueUsageSnapshot

#: 用量头前缀（大小写不敏感匹配）
WEIGHT_PREFIXES = ("X-MBX-USED-WEIGHT", "X-MBX-USED-WEIGHT-1M")
ORDER_COUNT_PREFIXES = ("X-MBX-ORDER-COUNT", "X-MBX-ORDER-COUNT-1M")


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """一次真实响应头的观测记录（审计用：证明了 collector 看过什么）。"""

    endpoint: str
    observed_at_ms: int
    header_names: tuple[str, ...]
    usage: VenueUsageSnapshot | None
    note: str


@dataclass(slots=True)
class VenueUsageCollector:
    """把真实响应头解析成 `VenueUsageSnapshot`（定义仍由别处提供）。"""

    clock: Callable[[], int]
    observations: list[UsageObservation] = field(default_factory=list)

    # ------------------------------------------------------------------ 采集

    def observe(self, *, endpoint: str, headers: Mapping[str, str],
                now_ms: int | None = None) -> UsageObservation:
        """解析一次真实响应头（无对应头 ⇒ usage=None，但仍留观测记录）。"""
        normalized = {str(name).upper(): str(value) for name, value in dict(headers or {}).items()}
        weight = _pick(normalized, WEIGHT_PREFIXES)
        orders = _pick(normalized, ORDER_COUNT_PREFIXES)
        usage = None
        if weight is not None or orders is not None:
            usage = VenueUsageSnapshot(
                used_weight=(None if weight is None else int(weight[1])),
                used_orders=(None if orders is None else int(orders[1])),
                reset_at_ms=None,                      # Binance 不在头里给 reset 时间 ⇒ 保持 UNKNOWN
                source=" + ".join(item[0] for item in (weight, orders) if item is not None),
                observed_at_ms=int(self.clock() if now_ms is None else now_ms))
            note = "usage parsed from real response headers"
        else:
            note = ("no usage headers present in this response "
                    "(checked: X-MBX-USED-WEIGHT*, X-MBX-ORDER-COUNT*)")
        observation = UsageObservation(endpoint=str(endpoint),
                                       observed_at_ms=int(self.clock() if now_ms is None else now_ms),
                                       header_names=tuple(sorted(normalized)),
                                       usage=usage, note=note)
        self.observations.append(observation)
        return observation

    # ------------------------------------------------------------------ 事实 / 证据

    def snapshot(self) -> VenueUsageSnapshot | None:
        """最近一次**解析出**的用量；从未解析出 ⇒ None（保持 UNKNOWN）。"""
        for observation in reversed(self.observations):
            if observation.usage is not None:
                return observation.usage
        return None

    def evidence(self) -> dict[str, object]:
        """采集证据：检查过多少响应、看过哪些头、是否出现过用量头。"""
        seen: set[str] = set()
        for observation in self.observations:
            seen.update(observation.header_names)
        parsed = [item for item in self.observations if item.usage is not None]
        return {
            "responses_inspected": len(self.observations),
            "endpoints_checked": sorted({item.endpoint for item in self.observations}),
            "usage_headers_seen": sorted(name for name in seen
                                         if name.startswith(WEIGHT_PREFIXES + ORDER_COUNT_PREFIXES)),
            "observations_with_usage": len(parsed),
            "last_note": self.observations[-1].note if self.observations else
                         "collector has not inspected any response yet",
            "usage_known": self.snapshot() is not None,
        }

    def bind(self, fetcher: object) -> "HeaderCapturingFetcher":
        """包装既有 fetcher：转发调用 + 采集真实响应头（对调用方透明，不改语义）。"""
        return HeaderCapturingFetcher(fetcher=fetcher, collector=self)


@dataclass(slots=True)
class HeaderCapturingFetcher:
    """透明包装：调用底层 fetcher 后，从其 `last_headers` 采集 usage 事实。"""

    fetcher: object
    collector: VenueUsageCollector

    def send(self, *, method: str, url: str, headers: Mapping[str, str], timeout_s: float) -> object:
        result = self.fetcher.send(method=method, url=url, headers=headers, timeout_s=timeout_s)  # type: ignore[attr-defined]
        captured = getattr(self.fetcher, "last_headers", {}) or {}
        endpoint = _endpoint_of(str(url))
        try:
            self.collector.observe(endpoint=endpoint, headers=captured)
        except Exception:  # noqa: BLE001 - 采集失败绝不影响真实请求结果
            pass
        return result


def _pick(normalized: Mapping[str, str], prefixes: tuple[str, ...]) -> tuple[str, int] | None:
    candidates = [(name, value) for name, value in normalized.items()
                  if any(name.startswith(prefix) for prefix in prefixes)]
    if not candidates:
        return None
    # 多窗口时取最大值（保守：用最紧的窗口）
    name, raw = max(candidates, key=lambda item: _safe_int(item[1]))
    value = _safe_int(raw)
    return None if value is None else (name, value)


def _safe_int(raw: str) -> int | None:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _endpoint_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path or "/"


__all__ = ["HeaderCapturingFetcher", "ORDER_COUNT_PREFIXES", "UsageObservation",
           "WEIGHT_PREFIXES", "VenueUsageCollector"]
