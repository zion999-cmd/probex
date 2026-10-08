"""Deterministic multi-hour market fixture for the chart workbench / screenshots.

测试资产（非产品代码）：生成一段**可复现**的 3 小时盘口快照 + 聚合成交事件流，
用于真实启动 REPLAY/PAPER 后让 K 线聚合器产生多根 1m/5m/15m/1h K 线与成交量。
"""
from __future__ import annotations

import random
from pathlib import Path

from market.events.payloads import AggressorSide
from market.events.types import MarketEvent
from tests.support import BASE_TS, depth_snapshot_event, mark_price_event, trade_event, write_store

#: 与 trial 配置一致的时间窗口起点（便于 candle 对齐）
START_TS = BASE_TS
DEFAULT_HOURS = 3
DEFAULT_STEP_MS = 5_000
#: 注入 MARK_PRICE 事件的默认间隔（测试事实；仅当显式给出 mark_price 时生效）
DEFAULT_MARK_STEP_MS = 30_000


def build_market_events(*, hours: int = DEFAULT_HOURS, step_ms: int = DEFAULT_STEP_MS,
                        seed: int = 7, mark_price: float | None = None,
                        mark_step_ms: int = DEFAULT_MARK_STEP_MS) -> list[MarketEvent]:
    """随机游走价格 + 每步一笔成交 + 每步一个盘口快照（确定性）。

    `mark_price`（P0001.15 §12 / 人类裁决 1B–1C）：给出时按 `mark_step_ms` 注入**明确**的
    `MARK_PRICE` 事件 —— 这是**测试市场事实**，不是把 last trade / mid 转换成 MARK。
    `None` ⇒ 不含任何 MARK 事件（用于验证 UNKNOWN → fail-closed 路径）。
    """
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
        # 真实微观结构：主动方**打到盘口**成交（buy 吃 ask / sell 砸 bid），而不是在价差内部成交。
        # 这一点很重要：P0001.17 的本地模拟成交（队列近似）按"成交价穿越挂单价"判定，
        # 若成交价恒在价差内部，touch 报价永远不会成交（本地闭环会假性无成交）。
        aggressor = AggressorSide.BUY if rng.random() > 0.5 else AggressorSide.SELL
        trade_price = ask if aggressor is AggressorSide.BUY else bid
        events.append(trade_event(trade_id, price=round(trade_price, 1), quantity=quantity,
                                  aggressor=aggressor, exchange_ts=ts, receive_ts=ts, process_ts=ts))
        trade_id += 1
        bids = [(round(bid - offset * 0.1, 1), round(abs(rng.gauss(1.0, 0.5)) + 0.05, 4))
                for offset in range(5)]
        asks = [(round(ask + offset * 0.1, 1), round(abs(rng.gauss(1.0, 0.5)) + 0.05, 4))
                for offset in range(5)]
        events.append(depth_snapshot_event(update_id, bids=bids, asks=asks, exchange_ts=ts,
                                           receive_ts=ts, process_ts=ts))
        update_id += 1
        if mark_price is not None and mark_step_ms > 0 and index % max(1, mark_step_ms // step_ms) == 0:
            events.append(mark_price_event(price=mark_price, exchange_ts=ts, receive_ts=ts, process_ts=ts))
    return events


def write_market_store(path: Path, *, hours: int = DEFAULT_HOURS,
                       step_ms: int = DEFAULT_STEP_MS, seed: int = 7,
                       mark_price: float | None = None,
                       mark_step_ms: int = DEFAULT_MARK_STEP_MS) -> tuple[int, int]:
    events = build_market_events(hours=hours, step_ms=step_ms, seed=seed, mark_price=mark_price,
                                mark_step_ms=mark_step_ms)
    write_store(path, events)
    return len(events), 2 * int(hours * 3_600_000 / step_ms)


__all__ = ["DEFAULT_HOURS", "DEFAULT_MARK_STEP_MS", "DEFAULT_STEP_MS", "START_TS", "build_market_events",
           "write_market_store"]
