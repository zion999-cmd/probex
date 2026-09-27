"""P0001.1 验收场景：确定性的交易所报文脚本。

一份连续快照 + 增量脚本，以及一个「快照偏旧」的恢复快照。所有事件都经由
Binance 归一化层构造，因此集成 / 故障 / 重放测试同时覆盖 SC-1。

故障路径：跳过增量 106、107 会令 108 触发 gap；108 本身不被缓冲，
109/110 在 STALE 期间被缓冲。恢复快照锚定在 108（含 106、107 的变化），
因此恢复时 109/110 可直接续上。

参考状态（无故障路径的终点）：

```text
bids: 99.5 x 4.0 , 98.0 x 1.0
asks: 102.0 x 2.5
last_update_id: 110
```
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import MarketEvent
from tests.support import BASE_TS, depth_diff_event, depth_snapshot_event

#: 增量脚本首尾序号。
FIRST_SCRIPTED_UPDATE_ID = 101
LAST_SCRIPTED_UPDATE_ID = 110

#: gap 发生前的最后一个已应用序号（脚本中跳过 106、107）。
UPDATE_ID_BEFORE_GAP = 105

#: 恢复快照覆盖到的序号（含 gap 区间 106、107 的变化）。
RECOVERY_SNAPSHOT_LAST_UPDATE_ID = 108


@dataclass(frozen=True, slots=True)
class DeltaScript:
    """一条增量报文的档位变化。"""

    update_id: int
    bids: tuple[tuple[float, float], ...] = ()
    asks: tuple[tuple[float, float], ...] = ()


SCRIPTED_DELTAS: tuple[DeltaScript, ...] = (
    DeltaScript(101, bids=((100.0, 2.0),)),  # 改量
    DeltaScript(102, asks=((100.5, 0.0),)),  # 删档
    DeltaScript(103, bids=((99.0, 5.0),)),  # 新增
    DeltaScript(104, asks=((101.0, 1.0),)),  # 改量
    DeltaScript(105, bids=((100.0, 0.0),)),  # 删档
    DeltaScript(106, bids=((99.5, 4.0),)),  # gap 区间
    DeltaScript(107, asks=((102.0, 2.5),)),  # gap 区间
    DeltaScript(108, bids=((98.0, 1.0),)),  # gap 之后到达
    DeltaScript(109, asks=((101.0, 0.0),)),  # gap 之后到达
    DeltaScript(110, bids=((99.0, 0.0),)),  # gap 之后到达
)


def _timestamps(update_id: int) -> dict[str, int]:
    return {
        "exchange_ts": BASE_TS + update_id,
        "receive_ts": BASE_TS + update_id + 1,
        "process_ts": BASE_TS + update_id + 2,
    }


def initial_snapshot() -> MarketEvent:
    """序号 100 的初始深度快照。"""
    return depth_snapshot_event(
        100,
        bids=[(100.0, 1.0), (99.5, 2.0)],
        asks=[(100.5, 1.5), (101.0, 3.0)],
        **_timestamps(100),
    )


def recovery_snapshot() -> MarketEvent:
    """序号 108 的恢复快照：已包含 gap 区间（106、107）与 108 自身的变化。"""
    return depth_snapshot_event(
        RECOVERY_SNAPSHOT_LAST_UPDATE_ID,
        bids=[(99.5, 4.0), (99.0, 5.0), (98.0, 1.0)],
        asks=[(101.0, 1.0), (102.0, 2.5)],
        **_timestamps(RECOVERY_SNAPSHOT_LAST_UPDATE_ID),
    )


def delta_event(update_id: int) -> MarketEvent:
    """按脚本构造单条增量事件。"""
    script = _script(update_id)
    return depth_diff_event(
        script.update_id,
        script.update_id,
        bids=script.bids,
        asks=script.asks,
        **_timestamps(update_id),
    )


def delta_events(*update_ids: int) -> list[MarketEvent]:
    return [delta_event(update_id) for update_id in update_ids]


def _script(update_id: int) -> DeltaScript:
    for script in SCRIPTED_DELTAS:
        if script.update_id == update_id:
            return script
    raise KeyError(f"no scripted delta for update id {update_id}")


def reference_events() -> list[MarketEvent]:
    """无故障参考路径：快照 100 + 全部增量 101..110。"""
    return [initial_snapshot(), *delta_events(*range(FIRST_SCRIPTED_UPDATE_ID, LAST_SCRIPTED_UPDATE_ID + 1))]


def events_before_gap() -> list[MarketEvent]:
    """gap 之前应该被正常应用的路径：快照 100 + 增量 101..105。"""
    return [initial_snapshot(), *delta_events(*range(FIRST_SCRIPTED_UPDATE_ID, UPDATE_ID_BEFORE_GAP + 1))]


def reference_bids() -> tuple[tuple[float, float], ...]:
    return ((99.5, 4.0), (98.0, 1.0))


def reference_asks() -> tuple[tuple[float, float], ...]:
    return ((102.0, 2.5),)
