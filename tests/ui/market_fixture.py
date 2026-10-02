"""Deterministic multi-hour market fixture for the chart workbench / screenshots.

测试资产（非产品代码）：生成一段**可复现**的 3 小时盘口快照 + 聚合成交事件流，
用于真实启动 REPLAY/PAPER 后让 K 线聚合器产生多根 1m/5m/15m/1h K 线与成交量。
"""
from __future__ import annotations

import random
from pathlib import Path

from market.events.payloads import AggressorSide
from market.events.types import MarketEvent
from tests.support import BASE_TS, depth_snapshot_event, trade_event, write_store

#: 与 trial 配置一致的时间窗口起点（便于 candle 对齐）
START_TS = BASE_TS
DEFAULT_HOURS = 3
DEFAULT_STEP_MS = 5_000


def build_market_events(*, hours: int = DEFAULT_HOURS, step_ms: int = DEFAULT_STEP_MS,
                        seed: int = 7) -> list[MarketEvent]:
    """随机游走价格 + 每步一笔成交 + 每步一个盘口快照（确定性）。"""
    rng = random.Random(seed)
    price = 60_000.0
    events: list[MarketEvent] = []
    update_id = 1
    trade_id = 1
    steps = int(hours * 3_600_000 / step_ms)
    for index in range(steps):
        ts = START_TS + index * step_ms
        drift = rng.gauss(0.0, 12.0)
        price = max(1_000.0, price + drift + (60.0 if index % 37 == 0 else 0.0) - (55.0 if index % 53 == 0 else 0.0))
        spread = max(0.5, abs(rng.gauss(1.0, 0.35)))
        bid = round(price - spread / 2, 1)
        ask = round(price + spread / 2, 1)
        quantity = round(abs(rng.gauss(0.6, 0.35)) + 0.05, 4)
        events.append(trade_event(trade_id, price=round(price, 1), quantity=quantity,
                                  aggressor=(AggressorSide.BUY if rng.random() > 0.5
                                             else AggressorSide.SELL),
                                  exchange_ts=ts, receive_ts=ts, process_ts=ts))
        trade_id += 1
        bids = [(round(bid - offset * 0.1, 1), round(abs(rng.gauss(1.0, 0.5)) + 0.05, 4))
                for offset in range(5)]
        asks = [(round(ask + offset * 0.1, 1), round(abs(rng.gauss(1.0, 0.5)) + 0.05, 4))
                for offset in range(5)]
        events.append(depth_snapshot_event(update_id, bids=bids, asks=asks, exchange_ts=ts,
                                           receive_ts=ts, process_ts=ts))
        update_id += 1
    return events


def write_market_store(path: Path, *, hours: int = DEFAULT_HOURS,
                       step_ms: int = DEFAULT_STEP_MS, seed: int = 7) -> tuple[int, int]:
    events = build_market_events(hours=hours, step_ms=step_ms, seed=seed)
    write_store(path, events)
    return len(events), 2 * int(hours * 3_600_000 / step_ms)


__all__ = ["DEFAULT_HOURS", "DEFAULT_STEP_MS", "START_TS", "build_market_events", "write_market_store"]
