"""同步调用一个 prediction provider 的最小中立辅助（离线回测与实时运行共用）。

P0017.5：抽取自 `backtest/driver._predict`，使 `runtime/` 不再反向依赖 `backtest/`。

- provider 为 None ⇒ 返回 None（无预测，策略按 prediction=None 被动报价）；
- 每次调用使用独立 asyncio 事件循环（LocalTrial 内部同步，但多标的交错调用复用同一循环
  会残留调度状态导致 stale 误判）；
- provider 异常绝不伪造预测 ⇒ 返回 None（与既有失败语义一致）。

本模块不做网络策略、不做重试编排（那是 `prediction/runtime.py` 的职责）；它只适配
"在确定性步进里同步拿到一条 record"这一需要。
"""

from __future__ import annotations

import asyncio
from typing import Any


def invoke_provider_record(runtime: Any, state: Any, now: int) -> Any:
    """同步提交 MarketState，返回 PredictionRecord 或 None。

    `runtime` 也可以是 `(provider, clock)` 元组：先把可注入时钟锚定到 `now`。
    """
    if runtime is None:
        return None
    clock = None
    if isinstance(runtime, tuple):
        runtime, clock = runtime
    if clock is not None and hasattr(clock, "set"):
        clock.set(now)
    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(runtime.submit(state))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()
        return getattr(result, "record", None)
    except Exception:  # noqa: BLE001 - provider 失败不致命；不伪造预测
        return None


__all__ = ["invoke_provider_record"]
