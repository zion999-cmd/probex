"""私有流 telemetry（P0001.9.2 §0.7）。

纪律：**任何字段都不得包含凭据、签名或 listenKey 原文**；只放计数、时长与状态名。
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True, slots=True)
class LatencyDistribution:
    """有界样本上的延迟分布（毫秒）。"""

    samples: int
    minimum: int
    median: int
    p95: int
    maximum: int



def latency_distribution(samples: Iterable[int]) -> LatencyDistribution | None:
    """由样本计算 min / median / p95 / max（空样本返回 None）。"""
    ordered = sorted(int(sample) for sample in samples)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return LatencyDistribution(
        samples=len(ordered),
        minimum=ordered[0],
        median=int(statistics.median(ordered)),
        p95=ordered[index],
        maximum=ordered[-1],
    )


@dataclass
class PrivateStreamCounters:
    """可变计数器。"""

    connect_count: int = 0
    reconnect_count: int = 0
    disconnect_count: int = 0
    timeout_count: int = 0
    message_count: int = 0
    heartbeat_count: int = 0
    account_update_count: int = 0
    order_update_count: int = 0
    fill_observation_count: int = 0
    listen_key_expired_count: int = 0
    listen_key_created_count: int = 0
    keepalive_count: int = 0
    keepalive_failure_count: int = 0
    duplicate_count: int = 0
    out_of_order_count: int = 0
    unsupported_event_count: int = 0
    malformed_count: int = 0
    snapshot_count: int = 0
    snapshot_failure_count: int = 0
    snapshot_round_trip_ms: int | None = None
    server_time_offset_ms: int | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class PrivateStreamTelemetry:
    """不可变 telemetry 快照（供验收与研究判断 private 链路是否可信）。"""

    listen_key_state: str
    continuity_assumed: bool
    last_receive_lag_ms: int | None
    private_lag_ms: LatencyDistribution | None
    connect_count: int
    reconnect_count: int
    disconnect_count: int
    timeout_count: int
    message_count: int
    heartbeat_count: int
    account_update_count: int
    order_update_count: int
    fill_observation_count: int
    listen_key_expired_count: int
    listen_key_created_count: int
    keepalive_count: int
    keepalive_failure_count: int
    duplicate_count: int
    out_of_order_count: int
    unsupported_event_count: int
    malformed_count: int
    snapshot_count: int
    snapshot_failure_count: int
    snapshot_round_trip_ms: int | None
    server_time_offset_ms: int | None
    last_error: str | None


@dataclass
class LatencySamples:
    """有界延迟样本缓冲。"""

    limit: int
    samples: deque[int] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError("LatencySamples.limit must be an int >= 1")
        self.samples = deque(maxlen=self.limit)

    def add(self, value_ms: int) -> None:
        self.samples.append(int(value_ms))

    @property
    def last(self) -> int | None:
        return self.samples[-1] if self.samples else None

    def distribution(self) -> LatencyDistribution | None:
        return latency_distribution(self.samples)


__all__ = [
    "LatencyDistribution",
    "LatencySamples",
    "PrivateStreamCounters",
    "PrivateStreamTelemetry",
    "latency_distribution",
]
